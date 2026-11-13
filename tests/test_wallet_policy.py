import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

import bot


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.responses = []

    async def reply_text(self, text, **kwargs):
        self.responses.append(text)


class WalletPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot.PATH = os.path.join(self.tmp.name, "test.sqlite3")
        bot.ADMINS = {1001}
        bot.Q = asyncio.Queue()
        bot.db().close()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def update(self, uid):
        message = FakeMessage("https://services.sheerid.com/verify/example?verificationId=example")
        return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=uid))

    def latest_order(self, uid):
        with bot.closing(bot.db()) as con:
            return con.execute("SELECT * FROM orders WHERE uid=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()

    async def test_guest_without_funds_is_blocked(self):
        update = self.update(2002)
        await bot.intake(update, None)
        self.assertIn("TOP-UP REQUIRED", update.message.responses[0])
        self.assertIsNone(self.latest_order(2002))
        self.assertTrue(bot.Q.empty())

    async def test_admin_can_submit_and_succeed_at_zero_wallet(self):
        update = self.update(1001)
        await bot.intake(update, None)
        row = self.latest_order(1001)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(bot.finish(row["id"], "success", "https://one.google.com/example"), "success")
        self.assertEqual(bot.balance(1001)[0], 0)
        self.assertIn("ADMIN TEST MODE", bot.verify_text(1001))
        self.assertIn("UNLIMITED", bot.catalog_text(1001).upper())

    async def test_customer_is_charged_only_after_success(self):
        uid = 3003
        bot.user(uid)
        with bot.closing(bot.db()) as con:
            con.execute("UPDATE users SET balance=500 WHERE uid=?", (uid,))
            con.commit()

        first = self.update(uid)
        await bot.intake(first, None)
        order1 = self.latest_order(uid)
        self.assertEqual(bot.balance(uid)[0], 500)
        self.assertEqual(bot.finish(order1["id"], "failed"), "failed")
        self.assertEqual(bot.balance(uid)[0], 500)

        second = self.update(uid)
        await bot.intake(second, None)
        order2 = self.latest_order(uid)
        self.assertEqual(bot.finish(order2["id"], "success", "https://one.google.com/example"), "success")
        self.assertEqual(bot.balance(uid)[0], 250)

    def test_catalog_and_guide_have_copy_link_instructions(self):
        bot.user(4004)
        self.assertIn("one.google.com/ai-student", bot.guide_text())
        self.assertIn("services.sheerid.com/verify/", bot.catalog_text(4004))
        self.assertIn("2.50", bot.catalog_text(4004))
        self.assertIn("TOP UP", bot.verify_text(4004).upper())


if __name__ == "__main__":
    unittest.main()
