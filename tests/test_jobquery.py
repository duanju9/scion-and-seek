#!/usr/bin/env python3
"""jobquery 回归测试：锁住「页面/导出/API 同一口径」这件事。

跑法：python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import jobquery  # noqa: E402


def job(**kw):
    base = {
        "job_id": kw.get("job_id", "j1"),
        "title": "AI 产品经理",
        "salary": "20-30K",
        "location": "深圳·南山区",
        "boss_name": "某某科技",
        "company_scale": "100-499人",
        "company_industry": "互联网",
        "boss_active_status": "刚刚活跃",
        "job_labels": "3-5年 | 本科",
        "skills": "大模型 | 数据分析",
        "source_keyword": "AI 产品经理",
        "jd": "负责大模型产品落地",
        "first_seen": "2026-11-26 06:00:00",
        "job_link": "https://example.com/1",
    }
    base.update(kw)
    return base


class NormalizeTest(unittest.TestCase):
    def test_hk_normalized(self):
        r = jobquery.normalize(job(location="香港·中西区"))
        self.assertEqual(r["city"], "中国香港")
        self.assertEqual(r["area"], "中西区")

    def test_city_param_alias(self):
        """旧链接里写 city=香港 也要能命中，否则用户点收藏夹就是 0 条。"""
        f = jobquery.parse_filters({"city": "香港"})
        self.assertEqual(f["city"], "中国香港")

    def test_exp_edu_from_labels(self):
        r = jobquery.normalize(job(job_labels="5-10年 | 硕士"))
        self.assertEqual((r["exp"], r["edu"]), ("5-10年", "硕士"))

    def test_salary_mid(self):
        self.assertEqual(jobquery.salary_mid("20-30K·16薪"), 25.0)
        self.assertIsNone(jobquery.salary_mid("面议"))
        self.assertIsNone(jobquery.salary_mid("300-500元/天"))


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            jobquery.normalize(job(job_id="a", title="AI 产品经理", location="深圳·南山区",
                                   company_industry="互联网", salary="20-30K")),
            jobquery.normalize(job(job_id="b", title="售前工程师", location="北京·朝阳区",
                                   company_industry="计算机软件", salary="面议",
                                   skills="客户沟通 | 标书")),
            jobquery.normalize(job(job_id="c", title="解决方案工程师", location="香港·中西区",
                                   company_industry="人工智能", salary="40-60K")),
        ]

    def run_q(self, **params):
        f = jobquery.parse_filters(params)
        return [r["job_id"] for r in self.rows if jobquery.match(r, f)]

    def test_q_hits_industry(self):
        """q 必须能搜到行业：之前只有导出搜行业，页面搜不到，是两套实现。"""
        self.assertEqual(self.run_q(q="计算机软件"), ["b"])

    def test_q_hits_city(self):
        self.assertEqual(self.run_q(q="北京"), ["b"])

    def test_city_filter_hk(self):
        self.assertEqual(self.run_q(city="中国香港"), ["c"])
        self.assertEqual(self.run_q(city="香港"), ["c"])

    def test_ai_filter(self):
        # 标题/技能含 AI、大模型、方案… 的按启发式判定
        self.assertEqual(self.run_q(ai="1"), ["a", "c"])


class SortTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            jobquery.normalize(job(job_id="low", salary="10-12K")),      # 11
            jobquery.normalize(job(job_id="high", salary="60-80K")),     # 70
            jobquery.normalize(job(job_id="none", salary="面议")),
        ]

    def ids(self, rows):
        return [r["job_id"] for r in rows]

    def test_salary_desc_nulls_last(self):
        self.assertEqual(self.ids(jobquery.sort_rows(self.rows, "salary", "desc")),
                         ["high", "low", "none"])

    def test_salary_asc_nulls_last(self):
        """面议在升序里也不能跑到第一屏——这是之前页面的真实 bug。"""
        self.assertEqual(self.ids(jobquery.sort_rows(self.rows, "salary", "asc")),
                         ["low", "high", "none"])

    def test_title_sort_is_deterministic(self):
        rows = [jobquery.normalize(job(job_id="x", title="B")),
                jobquery.normalize(job(job_id="y", title="A"))]
        self.assertEqual(self.ids(jobquery.sort_rows(rows, "title", "asc")), ["y", "x"])


class StatsTest(unittest.TestCase):
    def test_stats(self):
        rows = [jobquery.normalize(job(job_id="a", salary="20-30K")),
                jobquery.normalize(job(job_id="b", salary="面议", location="北京·朝阳区"))]
        st = jobquery.stats(rows)
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["with_jd"], 2)
        self.assertEqual(st["cities"], {"深圳": 1, "北京": 1})


class LoadMasterTest(unittest.TestCase):
    def test_partial_json_raises_cleanly(self):
        """刷新写盘途中读到半截 JSON：不能崩在 JSONDecodeError，要给出可读错误。"""
        d = tempfile.mkdtemp()
        with open(os.path.join(d, jobquery.MASTER_NAME), "w", encoding="utf-8") as f:
            f.write('{"jobs": [{"job_id": "a"')
        with self.assertRaises(RuntimeError):
            jobquery.load_master(d, retries=2)

    def test_dict_and_list_shapes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, jobquery.MASTER_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"jobs": [job()]}, f, ensure_ascii=False)
        self.assertEqual(len(jobquery.load_master(d)), 1)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([job()], f, ensure_ascii=False)
        self.assertEqual(len(jobquery.load_master(d)), 1)


if __name__ == "__main__":
    unittest.main()
