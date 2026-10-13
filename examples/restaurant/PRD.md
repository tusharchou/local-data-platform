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

### 2. The tables of the example

`examples/restaurant` builds this data mart on a laptop, from synthetic data that a seeded generator writes. The two
tables above become the six that a restaurant system would export. The sales table is `orders` and `order_lines`.

| Table | Key | Columns |
|---|---|---|
| `restaurants` | `restaurant_id` | `name`, `location`, `cuisine`, `created_at` |
| `menu_items` | `menu_item_id` | `restaurant_id`, `name`, `category`, `price` |
| `customers` | `customer_id` | `name`, `city`, `signed_up_at` |
| `orders` | `order_id` | `restaurant_id`, `customer_id`, `order_ts`, `channel` (`dine_in`, `takeaway` or `delivery`), `total_amount`, `updated_at` |
| `order_lines` | `order_id`, `line_no` | `menu_item_id`, `quantity`, `unit_price` |
| `reviews` | `review_id` | `order_id`, `customer_id`, `restaurant_id`, `rating`, `comment`, `reviewed_at` |

Orders arrive in dated batches that every milestone reuses. Batch 1 covers January to June 2026. Batch 2 covers July
2026 and five late orders from the end of June, which were placed on time and only synced in July. After that comes
one drop file a day, from 1 to 7 August 2026. `updated_at` is when the restaurant system last changed an order, and
the models use it to tell the versions of an order apart.

**Churn.** A customer churns in the month they stop ordering. For month M, `customers_at_start` is the number of
customers with an order in month M minus 1, `churned` is the number of those customers with no order in month M, and
`churn_rate` is `churned` divided by `customers_at_start`, or null when `customers_at_start` is 0. The first month has
no row. The gold layer and the insights of later milestones use this definition.

### 3. Iceberg Configuration
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