"""Unit tests that need no database: cron, schedules, dump splitting, plans.

    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import datetime as dt
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MBM_DATA_DIR", tempfile.mkdtemp())

from mbm import engine  # noqa: E402
from mbm.tasks import Cron, schedule_to_cron  # noqa: E402

DUMP = b"""/*M!999999\\- enable the sandbox mode */
-- MariaDB dump 10.19
--
-- Host: localhost    Database:
-- ------------------------------------------------------
/*!40101 SET NAMES utf8mb4 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;

--
-- Current Database: `we``ird`
--

CREATE DATABASE /*!32312 IF NOT EXISTS*/ `we``ird` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;

USE `we``ird`;

--
-- Table structure for table `t1`
--

DROP TABLE IF EXISTS `t1`;
CREATE TABLE `t1` (`id` int(11) NOT NULL) ENGINE=InnoDB;

--
-- Dumping data for table `t1`
--

INSERT INTO `t1` VALUES
(1),
(2);
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50003 TRIGGER tr BEFORE INSERT ON t1 FOR EACH ROW SET NEW.id = NEW.id */;;
DELIMITER ;

--
-- Temporary table structure for view `v1`
--

/*!50001 CREATE VIEW `v1` AS SELECT 1 AS `id` */;

--
-- Dumping routines for database 'we''ird'
--

--
-- Current Database: `we``ird`
--

USE `we``ird`;

--
-- Final view structure for view `v1`
--

/*!50001 VIEW `v1` AS select `t1`.`id` AS `id` from `t1` */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;

-- Dump completed on 2026-11-03 17:04:34
"""


class CronTests(unittest.TestCase):
    def test_daily(self):
        c = Cron("30 2 * * *")
        self.assertEqual(c.next_after(dt.datetime(2026, 11, 3, 1, 0)), dt.datetime(2026, 11, 3, 2, 30))
        self.assertEqual(c.next_after(dt.datetime(2026, 11, 3, 2, 30)), dt.datetime(2026, 11, 4, 2, 30))

    def test_weekly_sunday_as_7(self):
        c = Cron("0 3 * * 7")  # Sunday
        n = c.next_after(dt.datetime(2026, 11, 3, 12, 0))  # a Tuesday
        self.assertEqual(n, dt.datetime(2026, 11, 8, 3, 0))
        self.assertEqual(n.weekday(), 6)

    def test_steps_and_ranges(self):
        c = Cron("*/15 9-17 * * 1-5")
        self.assertTrue(c.matches(dt.datetime(2026, 11, 3, 9, 45)))
        self.assertFalse(c.matches(dt.datetime(2026, 11, 3, 18, 0)))
        self.assertFalse(c.matches(dt.datetime(2026, 11, 8, 10, 0)))  # Sunday

    def test_dom_or_dow(self):
        c = Cron("0 0 1 * 1")  # the 1st, or any Monday
        self.assertTrue(c.matches(dt.datetime(2026, 11, 1, 0, 0)))
        self.assertTrue(c.matches(dt.datetime(2026, 11, 2, 0, 0)))
        self.assertFalse(c.matches(dt.datetime(2026, 11, 3, 0, 0)))

    def test_bad(self):
        for bad in ("", "* * * *", "61 * * * *", "* 24 * * *", "*/0 * * * *"):
            with self.assertRaises(ValueError):
                Cron(bad)

    def test_schedule_forms(self):
        self.assertEqual(schedule_to_cron({"mode": "manual"}), "")
        self.assertEqual(schedule_to_cron({"mode": "daily", "time": "02:05"}), "5 2 * * *")
        self.assertEqual(schedule_to_cron({"mode": "weekly", "time": "23:00", "days": [6, 0]}), "0 23 * * 0,6")
        self.assertEqual(schedule_to_cron({"mode": "hourly", "minute": 10, "every": 6}), "10 */6 * * *")
        self.assertEqual(schedule_to_cron({"mode": "monthly", "time": "04:00", "day": 15}), "0 4 15 * *")
        self.assertEqual(schedule_to_cron({"mode": "cron", "cron": " 1 2 3 4 5 "}), "1 2 3 4 5")


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.header, self.footer, self.dbs, _ = engine.split_dump(io.BytesIO(DUMP), self.dir)

    def test_layout(self):
        self.assertEqual(len(self.dbs), 1)
        db = self.dbs[0]
        self.assertEqual(db["name"], "we`ird")
        self.assertIn("CREATE DATABASE", db["create_sql"])
        kinds = [(s["kind"], s["object"]) for s in db["sections"]]
        self.assertEqual(kinds, [("table", "t1"), ("data", "t1"), ("triggers", "t1"),
                                 ("view_stub", "v1"), ("routines", "we'ird"), ("view", "v1")])
        self.assertIn(b"SET NAMES utf8mb4", self.header)
        self.assertIn(b"FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS", self.footer)
        self.assertNotIn(b"Dump completed", self.footer)

    def test_sections_hold_the_sql(self):
        import gzip
        db = self.dbs[0]
        read = lambda i: gzip.open(os.path.join(self.dir, db["sections"][i]["file"])).read()
        self.assertIn(b"CREATE TABLE `t1`", read(0))
        self.assertIn(b"(2);", read(1))
        self.assertNotIn(b"TRIGGER", read(1))
        self.assertIn(b"TRIGGER tr", read(2))
        self.assertIn(b"select `t1`.`id`", read(5))

    def test_selection(self):
        db = self.dbs[0]
        self.assertEqual(engine.db_objects(db), [("table", "t1"), ("view", "v1")])  # routines empty
        secs = engine.selected_sections(db, {("table", "t1")}, schema_only=True)
        self.assertEqual([s["kind"] for s in secs], ["table", "triggers"])

    def test_rename(self):
        sql = engine.rewrite_create_db(self.dbs[0]["create_sql"], "we`ird", "copy")
        self.assertIn("`copy`", sql)
        self.assertNotIn("we``ird", sql)


class AccountTests(unittest.TestCase):
    def test_grant_never_resets_password(self):
        norm = {"replace_users": False, "roles": [], "users": [{
            "user": "app", "host": "%", "create": "CREATE USER `app`@`%` IDENTIFIED BY PASSWORD '*AB'",
            "grants": ["GRANT USAGE ON *.* TO `app`@`%` IDENTIFIED BY PASSWORD '*AB' WITH MAX_QUERIES_PER_HOUR 5",
                       "GRANT ALL ON `x`.* TO `app`@`%` WITH GRANT OPTION"]}]}
        stmts = [s for s, _ in engine.account_statements(norm)]
        self.assertTrue(stmts[0].startswith("CREATE USER IF NOT EXISTS `app`@`%` IDENTIFIED BY PASSWORD"))
        self.assertEqual(stmts[1], "GRANT USAGE ON *.* TO `app`@`%` WITH MAX_QUERIES_PER_HOUR 5")
        self.assertEqual(stmts[2], "GRANT ALL ON `x`.* TO `app`@`%` WITH GRANT OPTION")
        norm["replace_users"] = True
        self.assertTrue(engine.account_statements(norm)[0][0].startswith("CREATE OR REPLACE USER"))


if __name__ == "__main__":
    unittest.main()
