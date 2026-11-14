import os
import tempfile
import unittest
from contextlib import closing
from cryptography.fernet import Fernet

import bot
import panel

class LivePanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        bot.PATH=os.path.join(self.tmp.name,"admin-panel.sqlite3")
        bot.db().close()
        panel.ensure_panel_tables(bot.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_original_provider_vip_free_and_points(self):
        text="💎 VIP Priority Turns: 10 turns\n🎁 Free / Standard Turns: 1 turns\n✨ Reward Points: 0 pts"
        self.assertEqual(panel.parse_credits(text),{"vip":10,"free":1,"points":0})

    def test_remaining_counter_style(self):
        text="📊 Remaining: 💎 10 VIP | 🎁 1 Free | Points: 4"
        data=panel.parse_credits(text)
        self.assertEqual(data["vip"],10)
        self.assertEqual(data["free"],1)

    def test_empty_provider_message_does_not_invent_balance(self):
        self.assertEqual(panel.parse_credits("Your request has been added to queue."),{})

    def test_encrypt_and_load_saved_phone_session(self):
        old=os.environ.get("PANEL_ENCRYPTION_KEY")
        os.environ["PANEL_ENCRYPTION_KEY"]=Fernet.generate_key().decode("ascii")
        try:
            secret="1AA-SUPER-SENSITIVE-SESSION"
            encrypted=panel._fernet().encrypt(secret.encode()).decode()
            with closing(bot.db()) as con:
                con.execute("INSERT INTO session_registry(account_id,label,source,secret) VALUES(?,?,?,?)",
                            (4321,"Mirror Test","panel",encrypted))
                con.commit()
            result=panel.load_saved_sessions(bot.db)
            self.assertEqual(result,[(secret,"Mirror Test",4321)])
            self.assertNotIn(secret,encrypted)
        finally:
            if old is None: os.environ.pop("PANEL_ENCRYPTION_KEY",None)
            else: os.environ["PANEL_ENCRYPTION_KEY"]=old

    def test_disabled_session_persists(self):
        with closing(bot.db()) as con:
            con.execute("INSERT INTO session_registry(account_id,label,source,enabled) VALUES(?,?,?,0)",
                        (123,"Test","env"))
            con.commit()
        self.assertTrue(panel.is_disabled(bot.db,123))
        self.assertFalse(panel.is_disabled(bot.db,444))

if __name__=="__main__":unittest.main()
