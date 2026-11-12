CREATE TABLE dbo.customers (
    customer_id INT NOT NULL PRIMARY KEY,
    full_name NVARCHAR(120) NOT NULL,
    email VARCHAR(200),
    ssn VARCHAR(20),
    segment VARCHAR(40),
    created_at DATETIME2 NOT NULL
);

CREATE TABLE dbo.products (
    product_id INT NOT NULL PRIMARY KEY,
    sku VARCHAR(40) NOT NULL,
    product_name NVARCHAR(150) NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL
);

CREATE TABLE dbo.orders (
    order_id INT NOT NULL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES dbo.customers(customer_id),
    product_id INT NOT NULL REFERENCES dbo.products(product_id),
    quantity INT NOT NULL,
    gross_amount DECIMAL(14,2) NOT NULL,
    discount DECIMAL(10,2),
    updated_at DATETIME2 NOT NULL
);

CREATE TABLE finance.gl_entries (
    entry_id INT NOT NULL PRIMARY KEY,
    order_id INT NOT NULL REFERENCES dbo.orders(order_id),
    account_code VARCHAR(20) NOT NULL,
    debit_amount DECIMAL(14,2) NOT NULL,
    credit_amount DECIMAL(14,2) NOT NULL,
    posted_at DATETIME2 NOT NULL
);

CREATE TABLE dbo.unused_temp_log (
    log_id INT NOT NULL PRIMARY KEY,
    message VARCHAR(500)
);

CREATE VIEW dbo.v_customer_orders AS
SELECT
    c.customer_id,
    c.full_name,
    COUNT(o.order_id) AS order_count,
    SUM(o.gross_amount - COALESCE(o.discount, 0)) AS net_spend
FROM dbo.customers c
JOIN dbo.orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.full_name;
