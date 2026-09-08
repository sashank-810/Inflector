You are the lead architect, senior full-stack engineer, quantitative researcher,
data engineer, and product designer for this project.

We are building a personal-use product called:

PROJECT INFLECTION

Full name:

Indian Equity Intelligence and Early Opportunity Detection System

This is NOT a stock tip application.

The purpose of the product is to continuously analyse the Indian listed equity
universe and identify companies undergoing meaningful business and financial
inflections before widespread market attention and institutional participation.

The system should produce a ranked RESEARCH OPPORTUNITY QUEUE.

The system must be evidence-driven, explainable, historically testable and
designed to avoid common quantitative research errors such as look-ahead bias.

================================================================

PRIMARY PRODUCT GOAL

================================================================

Analyse NSE and BSE listed equities and identify companies showing combinations
of:

1. Financial acceleration
2. Revenue acceleration
3. Profit acceleration
4. Margin expansion
5. Improving capital efficiency
6. Improving balance sheet
7. Strong cash flow quality
8. Business catalysts
9. Capacity expansion
10. New products
11. New markets
12. Large orders
13. Export expansion
14. Improving management execution
15. Low or moderate market attention
16. Reasonable valuation
17. Constructive market structure

The product must NOT claim to predict multibaggers.

The product should identify:

EARLY OPPORTUNITIES FOR RESEARCH.

================================================================

IMPORTANT PRODUCT PRINCIPLES

================================================================

1. PERSONAL USE ONLY

This is a single-user personal research system.

Do not build:

- multi-tenant SaaS
- subscriptions
- billing
- payments
- teams
- organizations
- complex RBAC

Keep authentication simple and appropriate for a single personal user.

---------------------------------------------------------------

2. DATA PROVIDER ABSTRACTION

Do NOT tightly couple the system to a specific website, scraper or data API.

Create an abstraction layer.

Example:

DataProvider

OfficialNSEProvider

OfficialBSEProvider

CSVProvider

ManualImportProvider

LicensedProvider

FutureProvider

Every provider must implement consistent interfaces.

The application should work even if a data provider is replaced.

Do not rely on undocumented or fragile scraping as the core architecture.

Data acquisition must be configurable.

---------------------------------------------------------------

3. POINT-IN-TIME DATA

This is mandatory.

For every financial or event dataset store:

fiscal_period

reported_date

published_date

available_date

revision_date

The backtesting engine must ONLY use information that was actually available
on the date being simulated.

Prevent look-ahead bias.

This is a non-negotiable requirement.

---------------------------------------------------------------

4. EXPLAINABILITY

Every score must explain itself.

The user should never see:

Score = 91

without understanding why.

Every score should include:

factor

contribution

direction

confidence

evidence

Example:

Financial Inflection +12
Revenue acceleration from 12% to 38%

Catalyst +8
Capacity expansion announced

Risk -5
Receivables increased faster than revenue

---------------------------------------------------------------

5. VERSION EVERYTHING

Scoring models must have versions.

Example:

Inflection Model v1.0

Every historical score must store:

score_version

Do not overwrite historical scores.

---------------------------------------------------------------

6. SEPARATE FACTS FROM AI INTERPRETATION

Financial calculations must be deterministic Python calculations.

Do not use LLMs to calculate:

Revenue Growth

PAT Growth

ROCE

Margins

Debt Ratios

Cash Flow Ratios

LLMs may be used for:

announcement classification

catalyst extraction

risk extraction

management commentary summarization

document summarization

However all AI outputs must include:

confidence

source

evidence

and should be reviewable.

---------------------------------------------------------------

7. TEST EVERYTHING

The project must include:

unit tests

integration tests

data validation tests

financial calculation tests

scoring tests

backtesting tests

API tests

frontend tests where appropriate

Add CI configuration.

---------------------------------------------------------------

8. DO NOT OVERENGINEER

This is personal-use software.

Use a clean architecture that can grow.

Avoid Kubernetes.

Avoid unnecessary microservices.

Avoid enterprise complexity.

Prefer modular monolith architecture.

================================================================

TECH STACK

================================================================

BACKEND

Python 3.12+

FastAPI

Pydantic

SQLAlchemy

Alembic

---------------------------------------------------------------

DATABASE

PostgreSQL

Use PostgreSQL for persistent operational data.

Use DuckDB for analytics, research and large historical analytical queries.

---------------------------------------------------------------

DATA

Polars preferred for large processing.

Pandas allowed where appropriate.

NumPy.

---------------------------------------------------------------

TASKS

Start simple.

Use APScheduler for scheduled tasks initially.

Architecture should allow migration to Celery/RQ later.

Do not introduce distributed task queues unless actually necessary.

---------------------------------------------------------------

FRONTEND

Next.js

TypeScript

Modern component architecture

Responsive desktop-first dashboard

Professional financial research interface

Dark mode default

Do not use excessive gradients or gimmicky UI.

The interface should feel like:

Bloomberg research tools

institutional research dashboard

quant research platform

but simplified for personal use.

---------------------------------------------------------------

DEPLOYMENT

Docker Compose

Services:

postgres

backend

worker

frontend

redis only if actually required

Include:

.env.example

development setup

production setup

backup strategy

================================================================

REPOSITORY STRUCTURE

================================================================

Create a clean monorepo.

Suggested structure:

project-inflection/

AGENTS.md

README.md

docker-compose.yml

Makefile

apps/

api/

worker/

web/

packages/

core/

data/

features/

scoring/

catalyst/

risk/

attention/

backtesting/

database/

migrations/

schemas/

tests/

docs/

architecture.md

data-sources.md

scoring.md

backtesting.md

research-methodology.md

scripts/

You may improve the structure if necessary, but preserve the separation of
responsibilities.

================================================================

CORE MODULES

================================================================

MODULE 1

COMPANY UNIVERSE

Maintain canonical company identities.

Handle:

NSE symbol

BSE code

ISIN

Company name

Legal name

Sector

Industry

Listing status

Multiple securities

Duplicate listings

Corporate actions

Company renaming

Mergers

Delistings

The canonical identifier should preferably be company/ISIN based rather than
exchange-symbol based.

================================================================

MODULE 2

MARKET DATA

Store:

date

open

high

low

close

adjusted close

volume

delivery quantity where available

delivery percentage

market capitalization where available

corporate action adjustments

Support:

daily data first

architecture ready for intraday later

================================================================

MODULE 3

FINANCIAL DATA

Support:

Quarterly

TTM

Annual

Store:

Revenue

Operating Revenue

Other Income

EBITDA

EBIT

Operating Profit

PAT

EPS

Exceptional Items

Tax

Interest

Gross Margin where available

Also balance sheet:

Assets

Liabilities

Equity

Debt

Cash

Inventory

Receivables

Payables

And cash flow:

Operating Cash Flow

Investing Cash Flow

Financing Cash Flow

Capex

Free Cash Flow

================================================================

MODULE 4

FINANCIAL FEATURE ENGINE

Calculate:

Revenue YoY Growth

Revenue QoQ Growth

Revenue CAGR

Revenue Acceleration

Revenue Growth Consistency

TTM Revenue Growth

PAT YoY Growth

PAT QoQ Growth

PAT Acceleration

EPS Growth

Operating Profit Growth

EBITDA Growth

Margin Expansion

ROE

ROCE

ROIC

Asset Turnover

Debt to Equity

Net Debt

Interest Coverage

Current Ratio

Quick Ratio

Working Capital

Cash Conversion Cycle

Receivable Days

Inventory Days

Payable Days

Operating Cash Flow

Free Cash Flow

PAT to CFO Conversion

CFO to EBITDA

CAPEX to Revenue

All calculations must have documented formulas.

================================================================

MODULE 5

FINANCIAL INFLECTION ENGINE

Detect:

Revenue acceleration

Profit acceleration

Margin expansion

ROCE improvement

ROE improvement

Debt reduction

Cash flow improvement

Working capital improvement

Examples:

Revenue Growth:

5%

12%

28%

45%

should score differently from:

40%

39%

41%

40%

because acceleration matters.

Create robust algorithms for:

Trend

Acceleration

Magnitude

Consistency

Persistence

Outlier Detection

Do not overreact to a single quarter.

================================================================

MODULE 6

EARNINGS QUALITY ENGINE

Detect possible:

One-time gains

Exceptional profits

Asset sales

Tax benefits

Unusual other income

Receivables growing faster than revenue

Inventory anomalies

Profit without cash flow

Aggressive working capital

Output:

Earnings Quality Score

Risk Flags

Evidence

================================================================

MODULE 7

BUSINESS CATALYST ENGINE

Analyse:

Corporate announcements

Quarterly reports

Annual reports

Investor presentations

Management commentary

Potential catalyst types:

Capacity Expansion

New Plant

New Product

New Geography

Large Order

New Customer

Export Expansion

Acquisition

Strategic Partnership

Regulatory Approval

Technology Development

Market Entry

Price Increase

Guidance Upgrade

For each catalyst calculate:

Catalyst Type

Direction

Materiality

Confidence

Source

Published Date

Available Date

Supporting Evidence

Materiality must consider company size.

Example:

New Order Value

divided by

Annual Revenue

================================================================

MODULE 8

RISK ENGINE

Risk categories:

Financial Risk

Governance Risk

Liquidity Risk

Valuation Risk

Execution Risk

Customer Concentration Risk

Cyclicality Risk

Dilution Risk

Promoter Pledge Risk

Working Capital Risk

Output:

Risk Score

Risk Flags

Severity

Evidence

================================================================

MODULE 9

MARKET ATTENTION ENGINE

Purpose:

Measure how much market attention a company is already receiving.

Potential inputs:

Institutional ownership

Mutual fund ownership

FII ownership

Analyst coverage

News frequency

Trading activity

Volume expansion

Search interest

Other available attention proxies

Do not assume all inputs are available initially.

Design the module so new features can be added.

The goal is to calculate:

Market Attention Score

and:

Opportunity Gap

which combines:

Business Improvement

Catalyst Strength

Market Attention

================================================================

MODULE 10

MARKET STRUCTURE ENGINE

Do NOT build a simple RSI screener.

Analyse:

Long Consolidation

Relative Strength

Relative Strength vs Benchmark

Relative Strength vs Sector

Volatility Contraction

Volume Trend

Delivery Trend

Higher Highs

Higher Lows

Moving Average Structure

Breakout Structure

Accumulation Proxies

Architecture should support later advanced technical research.

================================================================

MODULE 11

VALUATION ENGINE

Calculate where data permits:

PE

Forward PE architecture

PB

EV/EBITDA

EV/Sales

PEG

Market Cap / Sales

Enterprise Value

Valuation Percentile

Historical Valuation Range

Sector Comparison

Do not automatically treat low PE as good.

Valuation must be interpreted relative to:

Growth

Quality

Margins

Capital Efficiency

================================================================

MODULE 12

SCORING ENGINE

Create:

EARLY OPPORTUNITY SCORE

Initial components:

Financial Inflection 25%

Business Catalyst 20%

Business Quality 15%

Cash Flow Quality 10%

Balance Sheet 10%

Valuation 10%

Market Structure 5%

Low Market Attention 5%

These weights must be configurable.

Do not permanently hardcode them.

Store scoring configuration and version.

Example:

Inflection Model v1.0

Every score must store:

company_id

date

score_version

component scores

final score

confidence

explanations

================================================================

MODULE 13

SCORE EXPLANATION ENGINE

For every company generate:

Why score increased

Why score decreased

Top positive factors

Top negative factors

New catalysts

New risks

Change vs previous score

Example:

Score

72 → 86

Reasons:

+6 Revenue acceleration

+4 PAT acceleration

+3 Capacity expansion

+2 Margin expansion

-1 Valuation deterioration

================================================================

MODULE 14

WATCHLIST

Support:

Active Research

Monitor

Owned

Archived

For each stock allow personal notes:

Investment Thesis

Catalysts

Risks

What Needs To Happen

Invalidation Conditions

Personal Conviction

Research Notes

================================================================

MODULE 15

ALERT ENGINE

Avoid spam.

Alert types:

New High Score

Score Upgrade

Score Downgrade

New Catalyst

New Risk

Quarterly Inflection

Valuation Warning

New Watchlist Candidate

Alerts should include:

What happened

Why it matters

Evidence

Score change

Confidence

Initially support:

in-app alerts

database notifications

architecture for email/Telegram later

================================================================

MODULE 16

RESEARCH REPORT ENGINE

For every company generate a research page.

Include:

Business Overview

Financial Trend

Revenue

Profit

Margins

ROCE

Debt

Cash Flow

Catalysts

Risks

Valuation

Market Attention

Market Structure

Inflection Score

Score History

Score Explanation

Personal Notes

Raw Source Links

Timeline of Events

================================================================

MODULE 17

BACKTESTING ENGINE

This is critical.

Support point-in-time backtests.

At historical date T:

Use only information available at T.

Do not use future financial data.

Do not use revised future information unless available at T.

Prevent survivorship bias where possible.

Support:

Top N portfolios

Score thresholds

Rebalancing

Monthly

Quarterly

Annual

Holding periods

1 Year

3 Years

5 Years

Metrics:

Average Return

Median Return

Hit Rate

Maximum Drawdown

Volatility

Sharpe

Sortino

Benchmark Comparison

Alpha where feasible

Compare:

Top Score Decile

vs

Middle Decile

vs

Bottom Decile

================================================================

MODULE 18

HISTORICAL WINNER RESEARCH

Build tools to identify historical winners.

Examples:

3x within 3 years

5x within 5 years

For every winner analyse:

T - 24 months

T - 12 months

T - 6 months

T - 3 months

Measure:

Revenue

Profit

Margins

ROCE

Debt

Cash Flow

Valuation

Attention

Institutional Ownership

Market Structure

Compare against companies that looked promising but failed.

================================================================

MODULE 19

AI DOCUMENT INTELLIGENCE

Use AI only for unstructured information.

Capabilities:

Announcement Classification

Catalyst Extraction

Risk Extraction

Management Guidance Extraction

Annual Report Summarization

Investor Presentation Summarization

Management Promise Tracking

Every AI output must store:

Source

Evidence

Confidence

Model Metadata

Timestamp

Raw Text Reference

AI outputs should never silently modify financial calculations.

================================================================

DATABASE REQUIREMENTS

================================================================

Design a robust relational schema.

Core entities:

Company

Security

ExchangeListing

Price

CorporateAction

FinancialStatement

BalanceSheet

CashFlow

Announcement

Document

Catalyst

Risk

InstitutionalHolding

AttentionMetric

FeatureSnapshot

ScoreSnapshot

ScoreExplanation

WatchlistItem

ResearchNote

Alert

Backtest

BacktestRun

ModelVersion

ScoringConfiguration

All time-series data must be properly indexed.

================================================================

FRONTEND REQUIREMENTS

================================================================

Create a professional desktop-first dashboard.

Pages:

1.

Dashboard

Show:

New Inflections

Top Opportunities

Score Movers

New Catalysts

New Risks

Watchlist

Market Summary

---------------------------------------------------------------

2.

Opportunity Explorer

Search and filter entire stock universe.

Filters:

Score

Sector

Market Cap

Revenue Growth

Profit Growth

ROCE

Debt

Valuation

Attention

Risk

Catalyst

---------------------------------------------------------------

3.

Company Research Page

Show complete company intelligence.

---------------------------------------------------------------

4.

Inflection Radar

Visualize:

Financial Inflection

Catalyst

Quality

Risk

Attention

Valuation

Market Structure

---------------------------------------------------------------

5.

Watchlist

Personal research workflow.

---------------------------------------------------------------

6.

Alerts

Chronological event feed.

---------------------------------------------------------------

7.

Backtesting

Create and run experiments.

Show:

Performance

Drawdown

Hit Rate

Benchmark Comparison

---------------------------------------------------------------

8.

Research Notes

Personal investment research workspace.

================================================================

UI DESIGN

================================================================

Dark mode default.

Professional.

Minimal.

Data dense.

Readable.

Do not imitate Robinhood.

Do not make the interface gamified.

Avoid:

excessive gradients

huge cards

unnecessary animations

emoji-heavy UI

Use:

tables

charts

sparklines

trend indicators

score explanations

timelines

tooltips

drilldowns

================================================================

DATA QUALITY

================================================================

Build validation.

Detect:

Duplicate Companies

Duplicate Securities

Missing Financial Periods

Impossible Ratios

Negative Revenue Errors

Broken Dates

Outliers

Corporate Action Issues

Currency Unit Inconsistency

Lakh/Crore/Million Conversion Errors

Create a data quality dashboard.

================================================================

OBSERVABILITY

================================================================

Include:

Structured Logging

Job History

Data Ingestion Logs

Failure Tracking

Last Successful Update

Data Freshness

Provider Health

For every provider display:

Last Updated

Last Successful Fetch

Failure Count

Records Ingested

================================================================

SECURITY

================================================================

Personal use.

Keep secrets in environment variables.

Never commit:

API keys

Passwords

Tokens

Provide:

.env.example

================================================================

DOCUMENTATION

================================================================

Create:

README.md

Architecture Documentation

Data Source Documentation

Financial Formula Documentation

Scoring Methodology

Backtesting Methodology

Development Setup

Deployment Setup

Data Provider Guide

================================================================

DEVELOPMENT PROCESS

================================================================

IMPORTANT:

DO NOT attempt to blindly build everything at once.

Follow this process.

PHASE 0

Architecture and Design

First:

1. Inspect repository
2. Create AGENTS.md
3. Create architecture document
4. Create database design
5. Create module boundaries
6. Create implementation roadmap
7. Identify risks and assumptions
8. Present the plan

Do not proceed with massive implementation until architecture is coherent.

---------------------------------------------------------------

PHASE 1

Foundation

Implement:

Repository structure

Docker

PostgreSQL

DuckDB

Backend

Frontend shell

Database migrations

Core Company model

Security basics

Configuration

Logging

Testing

---------------------------------------------------------------

PHASE 2

Data Layer

Implement:

Provider abstraction

CSV provider

Manual import

Mock provider

Data ingestion pipeline

Company universe normalization

Price storage

Financial storage

Data validation

---------------------------------------------------------------

PHASE 3

Feature Engine

Implement:

Revenue features

Profit features

Margins

ROCE

Debt

Cash Flow

Trend

Acceleration

Unit tests for formulas

---------------------------------------------------------------

PHASE 4

Scoring

Implement:

Financial Inflection

Quality

Balance Sheet

Cash Flow

Risk

Valuation

Market Structure

Attention

Final Score

Score Explanation

---------------------------------------------------------------

PHASE 5

Dashboard

Implement:

Dashboard

Opportunity Explorer

Company Page

Score Explanation

Watchlist

---------------------------------------------------------------

PHASE 6

Catalyst Intelligence

Implement:

Announcement pipeline

Document storage

Rule-based classification

AI-ready abstraction

Catalyst extraction

Risk extraction

---------------------------------------------------------------

PHASE 7

Alerts

Implement:

Score change detection

New catalyst alerts

New risk alerts

In-app notifications

---------------------------------------------------------------

PHASE 8

Backtesting

Implement:

Point-in-time snapshots

Portfolio construction

Historical ranking

Performance metrics

Benchmark comparison

---------------------------------------------------------------

PHASE 9

Historical Research

Implement:

Winner identification

Reverse engineering

False positive comparison

Feature importance exploration

---------------------------------------------------------------

PHASE 10

Hardening

Implement:

Performance optimization

Data quality dashboard

Backup

Error handling

Security review

Documentation

End-to-end testing

================================================================

CODING STANDARDS

================================================================

Python:

Type hints required.

Pydantic models where appropriate.

Clear docstrings.

Small functions.

No giant files.

No hidden business logic.

Use domain-specific modules.

Frontend:

Strict TypeScript.

Reusable components.

Accessible UI.

Clear loading states.

Error states.

Empty states.

Do not use any "any" types unless unavoidable.

================================================================

QUALITY GATES

================================================================

Before completing each phase:

1. Run tests
2. Run linting
3. Run type checks
4. Test Docker environment
5. Test migrations
6. Verify API
7. Verify UI
8. Update documentation

Do not claim something works without testing it.

If blocked by external data access:

Create:

mock provider

sample dataset

clear provider interface

and continue building the product.

Do not fabricate real market data.

================================================================

CURRENT TASK

================================================================

START WITH PHASE 0.

Do the following:

1. Inspect the repository.

2. Create AGENTS.md containing project engineering rules.

3. Create:

docs/architecture.md

docs/data-model.md

docs/scoring-methodology.md

docs/implementation-roadmap.md

4. Design the complete database schema.

5. Create a dependency graph of modules.

6. Identify all external data interfaces required.

7. Identify data licensing and data quality risks.

8. Recommend the final architecture.

9. Present a concise implementation plan.

10. Ask for confirmation only if there is a major architectural decision that
cannot reasonably be made.

Do not build fake placeholder complexity.

Make sensible engineering decisions.

After Phase 0 is complete, prepare the repository for Phase 1 implementation.