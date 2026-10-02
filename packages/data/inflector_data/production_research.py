"""Profile-driven composition of persisted production evidence for V5 orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.balance_sheet_scoring import BalanceSheetEvidence
from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_RULESET_CODE,
    BUSINESS_EVENT_QUANT_RULESET_VERSION,
)
from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
)
from inflector_core.business_quality_scoring import BusinessQualityEvidence
from inflector_core.cash_flow_quality_scoring import CashFlowQualityEvidence
from inflector_core.component_scoring import FinancialInflectionEvidence
from inflector_core.scoring_policy import (
    ConfidenceInputs,
    EligibilityInputs,
    FinancialContextSelection,
    InflectionScoringPolicy,
)
from inflector_data.attention_features import (
    AttentionFeatureBundle,
    AttentionFeaturePrimitives,
    AttentionSeriesIdentity,
)
from inflector_data.attention_pit import PointInTimeAttentionReader
from inflector_data.business_event_features import BusinessEventFeaturePrimitives
from inflector_data.business_event_pit import PointInTimeBusinessEventReader
from inflector_data.business_event_quantitative_pit import (
    PointInTimeBusinessEventQuantitativeReader,
)
from inflector_data.capital_features import (
    CapitalEfficiencyFeatures,
    ReturnOnCapitalEmployedValue,
)
from inflector_data.cash_flow_features import CashFlowQualityFeatures
from inflector_data.corporate_action_pit import PointInTimeCorporateActionReader
from inflector_data.financial_features import (
    FinancialInflectionFeatures,
    GrowthAccelerationValue,
)
from inflector_data.financial_snapshots import InstantFinancialSnapshotReader
from inflector_data.growth_history_features import GrowthHistoryFeatures
from inflector_data.market_adjustments import MarketAdjustmentPrimitives
from inflector_data.market_pit import PointInTimeMarketReader
from inflector_data.market_structure_features import (
    IndependentDeliveryEvidence,
    MarketStructureFeatureBundle,
    MarketStructureFeaturePrimitives,
)
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.research_profile import ProductionResearchProfile
from inflector_data.score_orchestration import (
    BusinessCatalystContextCandidate,
    CrossDomainContextCandidate,
)
from inflector_data.ttm import TrailingTwelveMonthNormalizer
from inflector_data.valuation_features import ValuationFeaturePrimitives
from inflector_database.models import ExchangeListing, Security


class ProductionResearchAssemblyError(RuntimeError):
    """Explicit current-research identity or provider evidence is incoherent."""


@dataclass(frozen=True, slots=True)
class ProductionSecurityContext:
    security: Security
    listing: ExchangeListing


@dataclass(frozen=True, slots=True)
class ProductionResearchEvidence:
    identity: ProductionSecurityContext
    eligibility_inputs: EligibilityInputs
    confidence_inputs: ConfidenceInputs
    cross_domain_context_candidates: tuple[CrossDomainContextCandidate, ...]
    business_catalyst_context_candidates: tuple[BusinessCatalystContextCandidate, ...]
    market_structure_evidence: MarketStructureFeatureBundle | None
    low_market_attention_evidence: AttentionFeatureBundle | None
    attempted_financial_contexts: tuple[tuple[UUID, str], ...]
    unavailable_reasons: dict[str, str]
    attention_news_status: str
    analyst_attention_status: str
    delivery_status: str
    delivery_observation_count: int
    delivery_pit_cutoff: datetime | None


class ProductionResearchAssembler:
    """Read persisted facts and call only accepted PIT feature primitives."""

    def __init__(self, session: Session, profile: ProductionResearchProfile) -> None:
        self._session = session
        self._profile = profile
        self._financial_reader = PointInTimeFinancialReader(session)
        self._quarters = FiscalQuarterNormalizer(self._financial_reader)
        self._financial_features = FinancialInflectionFeatures(self._quarters)
        self._growth = GrowthHistoryFeatures(self._financial_features)
        self._ttm = TrailingTwelveMonthNormalizer(self._quarters)
        self._snapshots = InstantFinancialSnapshotReader(self._financial_reader)
        self._capital = CapitalEfficiencyFeatures(self._snapshots, self._ttm)
        self._cash_flow = CashFlowQualityFeatures(self._snapshots, self._ttm)
        self._market_reader = PointInTimeMarketReader(session)

    def resolve_symbol(self, symbol: str) -> ProductionSecurityContext:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("symbol must be non-empty")
        matches = list(
            self._session.scalars(
                select(ExchangeListing).where(
                    ExchangeListing.exchange == "NSE",
                    ExchangeListing.symbol == normalized,
                    ExchangeListing.valid_to.is_(None),
                )
            )
        )
        if len(matches) != 1:
            raise ProductionResearchAssemblyError(
                "symbol must resolve to exactly one current NSE listing"
            )
        listing = matches[0]
        security = self._session.get(Security, listing.security_id)
        if security is None:
            raise ProductionResearchAssemblyError("listing security is unavailable")
        return ProductionSecurityContext(security=security, listing=listing)

    def assemble(
        self,
        *,
        symbol: str,
        fiscal_year: int,
        fiscal_quarter: int,
        knowledge_cutoff: datetime,
        dataset_ids: dict[str, UUID | None],
        policy: InflectionScoringPolicy,
    ) -> ProductionResearchEvidence:
        cutoff = _aware_utc(knowledge_cutoff, "knowledge_cutoff")
        if fiscal_quarter not in {1, 2, 3, 4}:
            raise ValueError("fiscal_quarter must be between 1 and 4")
        identity = self.resolve_symbol(symbol)
        security = identity.security
        company = security.company
        financial_id = _required_dataset(dataset_ids, "financial")
        market_id = _required_dataset(dataset_ids, "market")
        benchmark_id = _required_dataset(dataset_ids, "benchmark")
        action_id = _required_dataset(dataset_ids, "corporate_actions")
        event_id = _required_dataset(dataset_ids, "business_events")

        financial_candidates = tuple(
            self._financial_candidate(
                provider_dataset_id=financial_id,
                company_id=company.id,
                security_id=security.id,
                filing_scope=scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                market_provider_dataset_id=market_id,
                cutoff=cutoff,
                policy=policy,
            )
            for scope in self._profile.financial_scope_priority
        )
        business_candidates, event_count, event_available = self._business_candidates(
            financial_provider_dataset_id=financial_id,
            event_provider_dataset_id=event_id,
            company_id=company.id,
            cutoff=cutoff,
        )
        market_evidence = self._market_structure(
            market_provider_dataset_id=market_id,
            corporate_action_provider_dataset_id=action_id,
            benchmark_provider_dataset_id=benchmark_id,
            security_id=security.id,
            cutoff=cutoff,
            delivery_provider_dataset_id=dataset_ids.get("delivery"),
        )
        attention, news_status, analyst_status = self._attention(
            company_id=company.id,
            cutoff=cutoff,
            policy=policy,
            news_provider_dataset_id=dataset_ids.get("news_attention"),
        )
        core_available, core_times = self._financial_core_availability(
            provider_dataset_id=financial_id,
            company_id=company.id,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            cutoff=cutoff,
        )
        comparable_history = self._comparable_history(
            provider_dataset_id=financial_id,
            company_id=company.id,
            cutoff=cutoff,
        )
        liquidity, market_times, market_complete = self._liquidity(
            provider_dataset_id=market_id,
            security_id=security.id,
            cutoff=cutoff,
        )
        benchmark_times, benchmark_complete = self._benchmark_availability(
            provider_dataset_id=benchmark_id,
            cutoff=cutoff,
        )
        is_new_listing = (
            cutoff.date() - identity.listing.valid_from
        ).days <= self._profile.new_listing_horizon_days
        eligibility = EligibilityInputs(
            security_type=security.security_type,
            security_status=security.status,
            listing_status=identity.listing.status,
            comparable_yoy_observations=comparable_history,
            financial_core_available=core_available,
            financial_core_required=self._profile.financial_core_required,
            average_daily_traded_value_inr=liquidity,
            has_critical_data_quality_issue=False,
            is_new_listing=is_new_listing,
        )
        available_slots = {
            metric: metric in core_times for metric in self._profile.financial_core_metrics
        }
        available_slots.update(
            {
                "market_history": market_complete,
                "benchmark_history": benchmark_complete,
                "business_events": event_count > 0,
                "news_attention": news_status == "complete",
                "analyst_attention": analyst_status == "complete",
            }
        )
        evidence_times = [*core_times.values(), *market_times, *benchmark_times]
        if event_available is not None:
            evidence_times.append(event_available)
        if attention is not None and attention.news_mentions_count.available_at is not None:
            evidence_times.append(attention.news_mentions_count.available_at)
        confidence = ConfidenceInputs(
            required_feature_count=len(self._profile.confidence_required_feature_slots),
            available_feature_count=sum(
                available_slots.get(slot, False)
                for slot in self._profile.confidence_required_feature_slots
            ),
            source_reliability=self._profile.source_reliability,
            freshest_required_evidence_at=max(evidence_times) if evidence_times else None,
            knowledge_cutoff=cutoff,
            comparable_history_observations=comparable_history,
            evidence_confidence=None,
        )
        unavailable = {
            "valuation": "market_cap_missing",
        }
        delivery_feature = market_evidence.average_delivery_percentage_20
        delivery_manifest = delivery_feature.evidence
        delivery_count = (
            len(delivery_manifest.observations)
            if isinstance(delivery_manifest, IndependentDeliveryEvidence)
            else 0
        )
        delivery_status = "complete" if delivery_feature.value is not None else "unavailable"
        if delivery_status != "complete":
            unavailable["delivery"] = "delivery_data_missing"
        if analyst_status != "complete":
            unavailable["analyst_attention"] = "approved_source_not_configured"
        return ProductionResearchEvidence(
            identity=identity,
            eligibility_inputs=eligibility,
            confidence_inputs=confidence,
            cross_domain_context_candidates=financial_candidates,
            business_catalyst_context_candidates=business_candidates,
            market_structure_evidence=market_evidence,
            low_market_attention_evidence=attention,
            attempted_financial_contexts=tuple(
                (financial_id, scope) for scope in self._profile.financial_scope_priority
            ),
            unavailable_reasons=unavailable,
            attention_news_status=news_status,
            analyst_attention_status=analyst_status,
            delivery_status=delivery_status,
            delivery_observation_count=delivery_count,
            delivery_pit_cutoff=delivery_feature.available_at,
        )

    def _financial_candidate(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        security_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        market_provider_dataset_id: UUID,
        cutoff: datetime,
        policy: InflectionScoringPolicy,
    ) -> CrossDomainContextCandidate:
        context = FinancialContextSelection(
            provider_dataset_id=provider_dataset_id,
            filing_scope=filing_scope,
            provider_priority_index=0,
            scope_priority_index=self._profile.financial_scope_priority.index(filing_scope),
            fallback_used=filing_scope != self._profile.financial_scope_priority[0],
            selection_reason="production_profile_candidate",
        )
        financial_policy = policy.financial_inflection
        scoring = financial_policy.scoring
        financial = FinancialInflectionEvidence(
            company_id=company_id,
            context=context,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
            revenue_acceleration=self._acceleration(
                provider_dataset_id,
                company_id,
                filing_scope,
                "revenue",
                fiscal_year,
                fiscal_quarter,
                cutoff,
            ),
            pat_acceleration=self._acceleration(
                provider_dataset_id,
                company_id,
                filing_scope,
                "pat",
                fiscal_year,
                fiscal_quarter,
                cutoff,
            ),
            margin_expansion=(
                self._financial_features.margin_expansion_as_of(
                    provider_dataset_id=provider_dataset_id,
                    company_id=company_id,
                    filing_scope=filing_scope,
                    margin_code=scoring.margin_code,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    as_of=cutoff,
                )
                if scoring is not None
                else None
            ),
            current_roce=self._roce(
                provider_dataset_id, company_id, filing_scope, fiscal_year, fiscal_quarter, cutoff
            ),
            prior_year_roce=self._roce(
                provider_dataset_id,
                company_id,
                filing_scope,
                fiscal_year - 1,
                fiscal_quarter,
                cutoff,
            ),
            growth_consistency=(
                self._growth.growth_consistency_as_of(
                    provider_dataset_id=provider_dataset_id,
                    company_id=company_id,
                    filing_scope=filing_scope,
                    metric_code=scoring.growth_history_metric_code,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    window_size=financial_policy.consistency_window_size,
                    as_of=cutoff,
                )
                if scoring is not None
                else None
            ),
            growth_persistence=(
                self._growth.growth_persistence_as_of(
                    provider_dataset_id=provider_dataset_id,
                    company_id=company_id,
                    filing_scope=filing_scope,
                    metric_code=scoring.growth_history_metric_code,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    window_size=financial_policy.persistence_window_size,
                    threshold=financial_policy.persistence_threshold,
                    as_of=cutoff,
                )
                if scoring is not None
                else None
            ),
        )
        quality = BusinessQualityEvidence(
            company_id=company_id,
            context=context,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
            roce=self._roce(
                provider_dataset_id, company_id, filing_scope, fiscal_year, fiscal_quarter, cutoff
            ),
            roe=self._capital.roe_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=cutoff,
            ),
            margin=(
                self._financial_features.quarter_margin_as_of(
                    provider_dataset_id=provider_dataset_id,
                    company_id=company_id,
                    filing_scope=filing_scope,
                    margin_code=policy.business_quality.margin_code,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    as_of=cutoff,
                )
                if policy.business_quality is not None
                else None
            ),
        )
        cash_flow = CashFlowQualityEvidence(
            company_id=company_id,
            context=context,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
            cfo_conversion=self._cash_flow.cfo_conversion_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=cutoff,
            ),
            cfo_to_ebitda=self._cash_flow.cfo_to_ebitda_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=cutoff,
            ),
            receivable_days=self._cash_flow.receivable_days_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=cutoff,
            ),
            trade_working_capital_change=self._cash_flow.trade_working_capital_change_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=cutoff,
            ),
        )
        balance = BalanceSheetEvidence(
            company_id=company_id,
            context=context,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
            net_debt=self._capital.net_debt_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                as_of=cutoff,
            ),
            ttm_ebitda=self._ttm.ttm_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                metric_code="ebitda_reported",
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=cutoff,
            ),
            debt_to_equity=self._capital.debt_to_equity_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                as_of=cutoff,
            ),
            interest_coverage=self._capital.interest_coverage_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=cutoff,
            ),
        )
        valuation = ValuationFeaturePrimitives(
            self._session, self._market_reader, self._ttm, self._snapshots
        ).features_as_of(
            market_provider_dataset_id=market_provider_dataset_id,
            financial_provider_dataset_id=provider_dataset_id,
            security_id=security_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            interval=self._profile.market_interval,
            as_of=cutoff,
            market_on_or_before=cutoff.date(),
        )
        return CrossDomainContextCandidate(
            provider_dataset_id=provider_dataset_id,
            filing_scope=filing_scope,
            financial_inflection=financial,
            business_quality=quality,
            cash_flow_quality=cash_flow,
            balance_sheet=balance,
            valuation=valuation,
        )

    def _acceleration(
        self,
        provider: UUID,
        company: UUID,
        scope: str,
        metric: str,
        year: int,
        quarter: int,
        cutoff: datetime,
    ) -> GrowthAccelerationValue | None:
        return self._financial_features.growth_acceleration_as_of(
            provider_dataset_id=provider,
            company_id=company,
            filing_scope=scope,
            metric_code=metric,
            fiscal_year=year,
            fiscal_quarter=quarter,
            as_of=cutoff,
        )

    def _roce(
        self, provider: UUID, company: UUID, scope: str, year: int, quarter: int, cutoff: datetime
    ) -> ReturnOnCapitalEmployedValue | None:
        return self._capital.roce_as_of(
            provider_dataset_id=provider,
            company_id=company,
            filing_scope=scope,
            fiscal_year=year,
            fiscal_quarter=quarter,
            as_of=cutoff,
        )

    def _market_structure(
        self,
        *,
        market_provider_dataset_id: UUID,
        corporate_action_provider_dataset_id: UUID,
        benchmark_provider_dataset_id: UUID,
        security_id: UUID,
        cutoff: datetime,
        delivery_provider_dataset_id: UUID | None,
    ) -> MarketStructureFeatureBundle:
        adjustments = MarketAdjustmentPrimitives(
            self._market_reader, PointInTimeCorporateActionReader(self._session)
        )
        return MarketStructureFeaturePrimitives(self._market_reader, adjustments).features_as_of(
            market_provider_dataset_id=market_provider_dataset_id,
            corporate_action_provider_dataset_id=corporate_action_provider_dataset_id,
            benchmark_provider_dataset_id=benchmark_provider_dataset_id,
            benchmark_code=self._profile.benchmark_code,
            security_id=security_id,
            interval=self._profile.market_interval,
            as_of=cutoff,
            market_on_or_before=cutoff.date(),
            delivery_provider_dataset_id=delivery_provider_dataset_id,
            delivery_series="EQ",
            **(
                {"delivery_observation_window": self._profile.delivery_observation_window}
                if self._profile.delivery_observation_window is not None
                else {}
            ),
        )

    def _business_candidates(
        self,
        *,
        financial_provider_dataset_id: UUID,
        event_provider_dataset_id: UUID,
        company_id: UUID,
        cutoff: datetime,
    ) -> tuple[tuple[BusinessCatalystContextCandidate, ...], int, datetime | None]:
        events = PointInTimeBusinessEventReader(self._session).company_events_as_of(
            provider_dataset_id=event_provider_dataset_id,
            company_id=company_id,
            as_of=cutoff,
            ruleset_code=BUSINESS_EVENT_RULESET_CODE,
            ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
            start_date=cutoff.date() - timedelta(days=self._profile.catalyst_lookback_days),
            end_date=cutoff.date(),
        )
        if not events:
            return (), 0, None
        quantitative = PointInTimeBusinessEventQuantitativeReader(self._session)
        features = BusinessEventFeaturePrimitives(self._ttm)
        candidates = []
        for scope in self._profile.financial_scope_priority:
            bundles = tuple(
                features.features_as_of(
                    event=event,
                    quantitative_derivation=quantitative.facts_for_event(
                        event=event,
                        ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
                        ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
                    ),
                    financial_provider_dataset_id=financial_provider_dataset_id,
                    filing_scope=scope,
                    as_of=cutoff,
                )
                for event in events
            )
            candidates.append(
                BusinessCatalystContextCandidate(
                    financial_provider_dataset_id=financial_provider_dataset_id,
                    filing_scope=scope,
                    event_provider_dataset_id=event_provider_dataset_id,
                    event_bundles=bundles,
                )
            )
        return (
            tuple(candidates),
            len(events),
            max((event.source_available_at for event in events), default=None),
        )

    def _attention(
        self,
        *,
        company_id: UUID,
        cutoff: datetime,
        policy: InflectionScoringPolicy,
        news_provider_dataset_id: UUID | None,
    ) -> tuple[AttentionFeatureBundle | None, str, str]:
        configured = policy.low_market_attention
        if configured is None or news_provider_dataset_id is None:
            return None, "unavailable", "source_not_configured"
        window_end = cutoff
        window_start = window_end - timedelta(days=self._profile.attention_news_window_days)
        bundle = AttentionFeaturePrimitives(
            PointInTimeAttentionReader(self._session)
        ).features_as_of(
            company_id=company_id,
            security_id=None,
            company_level_only=True,
            as_of=cutoff,
            news_series=AttentionSeriesIdentity(
                provider_dataset_id=news_provider_dataset_id,
                scope_code=configured.news_series.scope_code,
                methodology_version=configured.news_series.methodology_version,
                measurement_definition_sha256=configured.news_series.measurement_definition_sha256,
            ),
            news_window_start_at=window_start,
            news_window_end_at=window_end,
            analyst_series=AttentionSeriesIdentity(
                provider_dataset_id=configured.analyst_series.provider_dataset_id,
                scope_code=configured.analyst_series.scope_code,
                methodology_version=configured.analyst_series.methodology_version,
                measurement_definition_sha256=(
                    configured.analyst_series.measurement_definition_sha256
                ),
            ),
            analyst_observation_on_or_before=None,
        )
        news_status = "complete" if bundle.news_mentions_count.value is not None else "unavailable"
        analyst_status = (
            "complete"
            if bundle.analyst_coverage_count.value is not None
            else "source_not_configured"
        )
        return bundle, news_status, analyst_status

    def _financial_core_availability(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        fiscal_year: int,
        fiscal_quarter: int,
        cutoff: datetime,
    ) -> tuple[int, dict[str, datetime]]:
        for scope in self._profile.financial_scope_priority:
            times: dict[str, datetime] = {}
            for metric in self._profile.financial_core_metrics:
                values = self._financial_reader.financial_series_as_of(
                    provider_dataset_id=provider_dataset_id,
                    company_id=company_id,
                    filing_scope=scope,
                    metric_code=metric,
                    as_of=cutoff,
                )
                matching = [
                    value
                    for value in values
                    if value.fiscal_period.fiscal_year == fiscal_year
                    and value.fiscal_period.fiscal_quarter == fiscal_quarter
                ]
                if matching:
                    times[metric] = max(item.available_at for item in matching)
            if times:
                return len(times), times
        return 0, {}

    def _comparable_history(
        self, *, provider_dataset_id: UUID, company_id: UUID, cutoff: datetime
    ) -> int:
        for scope in self._profile.financial_scope_priority:
            values = self._financial_features.growth_series_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=scope,
                metric_code=self._profile.comparable_history_metric,
                comparison_kind="yoy",
                as_of=cutoff,
            )
            if values:
                return len(values)
        return 0

    def _liquidity(
        self, *, provider_dataset_id: UUID, security_id: UUID, cutoff: datetime
    ) -> tuple[Decimal | None, list[datetime], bool]:
        bars = self._market_reader.market_series_as_of(
            provider_dataset_id=provider_dataset_id,
            security_id=security_id,
            interval=self._profile.market_interval,
            as_of=cutoff,
            start_date=cutoff.date() - timedelta(days=self._profile.market_observation_days),
            end_date=cutoff.date(),
        )
        selected = bars[-self._profile.liquidity_window_observations :]
        complete = len(selected) >= self._profile.liquidity_minimum_observations
        times = [bar.available_at for bar in selected]
        if not complete:
            return None, times, False
        average = sum(
            (bar.close_price * Decimal(bar.volume) for bar in selected), Decimal("0")
        ) / Decimal(len(selected))
        return average, times, True

    def _benchmark_availability(
        self, *, provider_dataset_id: UUID, cutoff: datetime
    ) -> tuple[list[datetime], bool]:
        bars = self._market_reader.benchmark_series_as_of(
            provider_dataset_id=provider_dataset_id,
            benchmark_code=self._profile.benchmark_code,
            interval=self._profile.market_interval,
            as_of=cutoff,
            start_date=cutoff.date() - timedelta(days=self._profile.market_observation_days),
            end_date=cutoff.date(),
        )
        selected = bars[-self._profile.liquidity_window_observations :]
        return (
            [bar.available_at for bar in selected],
            len(selected) >= self._profile.liquidity_minimum_observations,
        )


def _required_dataset(values: dict[str, UUID | None], code: str) -> UUID:
    value = values.get(code)
    if value is None:
        raise ProductionResearchAssemblyError(f"required dataset is unavailable: {code}")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
