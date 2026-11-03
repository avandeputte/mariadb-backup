DROP DATABASE IF EXISTS shop; DROP DATABASE IF EXISTS hr;
CREATE DATABASE shop CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE shop;
CREATE TABLE customers (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, email VARCHAR(200), notes TEXT, UNIQUE KEY uq_email (email), KEY idx_name (name)) ENGINE=InnoDB;
CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, customer_id INT NOT NULL, total DECIMAL(10,2), created DATETIME DEFAULT CURRENT_TIMESTAMP, KEY idx_cust (customer_id), CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES customers(id)) ENGINE=InnoDB;
CREATE TABLE audit (id INT AUTO_INCREMENT PRIMARY KEY, msg VARCHAR(255));
INSERT INTO customers (name,email,notes) VALUES ('Ann','ann@x.io','line1\nline2\ttab'),('Bob','bob@x.io',NULL),('Zoë','zoe@x.io','unicode ✓ \'quote\'');
INSERT INTO orders (customer_id,total) VALUES (1,10.50),(1,20.00),(2,5.25);
CREATE VIEW v_totals AS SELECT c.name, SUM(o.total) t FROM customers c JOIN orders o ON o.customer_id=c.id GROUP BY c.name;
CREATE VIEW v_big AS SELECT * FROM v_totals WHERE t > 10;
DELIMITER //
CREATE TRIGGER trg_ord AFTER INSERT ON orders FOR EACH ROW BEGIN INSERT INTO audit(msg) VALUES (CONCAT('order ',NEW.id)); END//
CREATE PROCEDURE p_count(OUT n INT) BEGIN SELECT COUNT(*) INTO n FROM orders; END//
CREATE FUNCTION f_double(x INT) RETURNS INT DETERMINISTIC RETURN x*2//
DELIMITER ;
CREATE EVENT ev_cleanup ON SCHEDULE EVERY 1 DAY DO DELETE FROM audit WHERE id < 0;
CREATE DATABASE hr; USE hr;
CREATE TABLE staff (id INT PRIMARY KEY, name VARCHAR(50), FULLTEXT KEY ft_name (name)) ENGINE=InnoDB;
INSERT INTO staff VALUES (1,'Carol'),(2,'Dan');
DROP USER IF EXISTS 'app'@'%', 'report'@'localhost'; DROP ROLE IF EXISTS readonly;
CREATE ROLE readonly; GRANT SELECT ON shop.* TO readonly;
CREATE USER 'app'@'%' IDENTIFIED BY 's3cret!'; GRANT SELECT, INSERT, UPDATE ON shop.* TO 'app'@'%'; GRANT SELECT (name) ON hr.staff TO 'app'@'%';
CREATE USER 'report'@'localhost' IDENTIFIED BY 'r3port'; GRANT readonly TO 'report'@'localhost'; SET DEFAULT ROLE readonly FOR 'report'@'localhost';
GRANT EXECUTE ON PROCEDURE shop.p_count TO 'report'@'localhost';
