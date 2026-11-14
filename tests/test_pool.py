import asyncio
import unittest
from types import SimpleNamespace
from pool import MirrorPool

class FakeMirror:
    def __init__(self):
        self.client=SimpleNamespace(is_connected=lambda:True)
        self.owner=None
        self.active_order=None
        self.lease_until=0
        self.orders=[]
        self.texts=[]
    def may_start(self,uid):
        return self.owner is None or self.owner==uid
    def current_user(self,uid):
        return self.owner==uid
    async def open(self,uid):
        if self.owner is not None and self.owner!=uid:return False
        self.owner=uid
        return True
    async def submit_order(self,uid,oid,url):
        if self.owner!=uid or self.active_order is not None:return False
        self.active_order=oid
        self.orders.append((uid,oid))
        return True
    async def release(self,uid):
        if self.owner!=uid or self.active_order is not None:return False
        self.owner=None
        return True
    def cancel(self,oid):
        if self.active_order==oid:self.active_order=None

class FakeBot:
    def __init__(self):self.messages=[]
    async def send_message(self,uid,text):
        self.messages.append((uid,text))

class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_mirrors_and_fifo_queue(self):
        mirrors=[FakeMirror(),FakeMirror()]
        bot=FakeBot()
        started=[]
        pool=MirrorPool(mirrors,bot,finished=lambda oid:True,started=started.append)
        self.assertTrue(await pool.open(101))
        self.assertTrue(await pool.open(102))
        self.assertFalse(await pool.open(103))
        self.assertFalse(await pool.open(104))
        self.assertEqual(pool.position(103),1)
        self.assertEqual(pool.position(104),2)
        self.assertTrue(await pool.release(101))
        self.assertEqual(pool.position(103),0)
        self.assertEqual(pool.owners[103],mirrors[0])
        self.assertEqual(pool.position(104),1)
        self.assertTrue(await pool.release(102))
        self.assertEqual(pool.owners[104],mirrors[1])
    async def test_order_waits_and_starts_after_release(self):
        m=FakeMirror()
        bot=FakeBot()
        started=[]
        pool=MirrorPool([m],bot,finished=lambda oid:True,started=started.append)
        await pool.open(1)
        self.assertEqual(await pool.submit_order(2,55,"link"),"queued")
        self.assertEqual(pool.position(2),1)
        await pool.release(1)
        self.assertEqual(m.orders,[(2,55)])
        self.assertEqual(started,[55])
        self.assertIn("processing",bot.messages[-1][1])

    async def test_completed_customer_rotates_behind_waiter(self):
        m=FakeMirror()
        bot=FakeBot()
        pool=MirrorPool([m],bot,finished=lambda oid:True)
        self.assertTrue(await pool.open(101))
        self.assertFalse(await pool.open(202))
        m.needs_rotation=True
        result=await pool.submit_order(101,88,"https://services.sheerid.com/verify/next")
        self.assertEqual(result,"queued")
        self.assertEqual(pool.owners.get(202),m)
        self.assertEqual(pool.position(101),1)
        self.assertNotIn(101,pool.owners)

if __name__=="__main__":unittest.main()
