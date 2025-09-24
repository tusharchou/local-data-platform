# Restaurant Data Mart - Product Requirements Document (PRD)

## Objective
To build a data mart for managing restaurant data, enabling seamless data collection, storage in Iceberg, and reporting to Slack through the LDP (Lakehouse Data Platform).

---

## Key Features

### 1. Data Collection
- **Source**: Collect data from restaurant management systems, APIs, and manual uploads.
- **Data Types**:
  - Restaurant details (name, location, cuisine, etc.)
  - Menu items and pricing
  - Sales and revenue data
  - Customer reviews and ratings
- **Validation**: Ensure data integrity with schema validation and deduplication.

### 2. Data Storage
- **Storage Format**: Apache Iceberg for efficient querying and versioning.
- **Partitioning**: Partition data by restaurant ID and date for optimized performance.
- **Metadata Management**: Maintain metadata for schema evolution and auditing.

### 3. Reporting and Notifications
- **Slack Integration**:
  - Send daily/weekly reports on key metrics (e.g., revenue, top-performing restaurants).
  - Alert on anomalies (e.g., sudden drop in sales).
- **Dashboards**: Provide visual insights into restaurant performance using BI tools.

### 4. Automation via LDP
- **ETL Pipelines**:
  - Extract data from sources.
  - Transform data into a unified schema.
  - Load data into Iceberg tables.
- **Scheduling**: Automate pipelines using LDP's orchestration capabilities.
- **Monitoring**: Track pipeline health and data quality metrics.

---

## Technical Requirements

### 1. Data Schema
- **Restaurant Table**:
  - `restaurant_id` (string, primary key)
  - `name` (string)
  - `location` (string)
  - `cuisine` (string)
  - `created_at` (timestamp)
- **Sales Table**:
  - `sale_id` (string, primary key)
  - `restaurant_id` (string, foreign key)
  - `amount` (decimal)
  - `sale_date` (date)
  - `created_at` (timestamp)

### 2. Iceberg Configuration
- Enable time travel and schema evolution.
- Optimize for large-scale queries.

### 3. Slack Integration
- Use Slack Webhooks for notifications.
- Configure channels for different types of reports.

---

## Success Metrics
- Reduced manual effort in managing restaurant data.
- Improved data accuracy and reporting timeliness.
- Increased adoption of insights by stakeholders.

---

## Timeline
- **Week 1-2**: Requirements gathering and schema design.
- **Week 3-4**: Build ETL pipelines and Iceberg integration.
- **Week 5**: Implement Slack reporting and dashboards.
- **Week 6**: Testing and deployment.

---

## Stakeholders
- **Product Owner**: [Your Name]
- **Engineering Team**: Data Engineers, Backend Developers
- **Business Team**: Restaurant Managers, Analysts