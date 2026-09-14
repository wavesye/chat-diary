import unittest

from diary_agent.writer import normalize_entry, render_markdown


class WriterTests(unittest.TestCase):
    def test_database_status_controls_checkboxes_and_completed_todo_is_not_reopened(self):
        result = render_markdown("2026-09-14", {
            "diary": "整理了一下手头的事情。",
            "todos": [
                {"title": "交报表", "status": "completed"},
                {"title": "回复邮件", "status": "active", "completed_at": "2026-09-14"},
                {"title": "未确认的猜测", "status": "pending_confirmation"},
                {"title": "已经取消的计划", "status": "cancelled"},
            ],
            "tomorrow": ["交报表", "回复邮件", "散步"],
        })
        self.assertEqual(result.count("- [x] 交报表"), 1)
        self.assertNotIn("- [ ] 交报表", result)
        self.assertEqual(result.count("- [ ] 回复邮件"), 1)
        self.assertNotIn("未确认的猜测", result)
        self.assertNotIn("已经取消的计划", result)
        self.assertIn("## 留给明天\n\n- [ ] 散步", result)

    def test_preserved_quotes_keep_whitespace_unicode_and_line_endings(self):
        first = "  原话，不能‘美化’。\r\n\r\n    Maybe tomorrow.  \r\n"
        second = "\n\t# 想说的话\n```\n原文里面的代码块\n```\n\n"
        source = {"title": "随手记", "diary": "记下了两个念头。",
                  "verbatim_quotes": [first, second], "quote": "原话，不能‘美化’。"}
        self.assertEqual(normalize_entry(source)["verbatim_quotes"], [first, second])
        result = render_markdown("2026-09-14", source)
        self.assertIn(("## 我的原话\n\n" + first + "\n\n" + second).encode(), result.encode())
        self.assertEqual(result.count(first), 1)
        self.assertEqual(result.count(second), 1)
        self.assertNotIn("> 原话", result)

    def test_decorated_model_title_stays_one_clean_heading(self):
        raw = {"title": '# 标题：**“2026-09-14｜把报表交了”**\n## 意外的小标题',
               "diary": "正文"}
        entry = normalize_entry(raw)
        self.assertEqual(entry["title"], "把报表交了")
        rendered = render_markdown("2026-09-14", raw)
        self.assertIn("\n# 把报表交了\n", rendered)
        self.assertNotIn("意外的小标题", rendered)
        self.assertEqual(normalize_entry({"title": "2026年9月14日"})["title"], "日常记录")

    def test_todo_title_cannot_create_extra_markdown_checkboxes(self):
        rendered = render_markdown("2026-09-14", {
            "todos": [{"title": "整理笔记\n- [x] 尚未完成", "status": "active"}],
        })
        self.assertIn("- [ ] 整理笔记 - [x] 尚未完成", rendered)
        self.assertNotIn("\n- [x]", rendered)


if __name__ == "__main__":
    unittest.main()
