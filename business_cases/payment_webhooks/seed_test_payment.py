import unittest
from payment import apply_event, new_store, order_view


class ExistingRegression(unittest.TestCase):
    def test_unknown_order(self):
        self.assertEqual(order_view(new_store(), 'order-a')['status'], 'pending')

    def test_capture(self):
        view = apply_event(new_store(), dict(event_id='e1', order_id='a', kind='capture', payment_id='p1', amount_cents=1000))
        self.assertEqual(view, dict(status='paid', captured_cents=1000, refunded_cents=0, fulfillments=1))

    def test_partial_refund(self):
        store = new_store()
        apply_event(store, dict(event_id='e1', order_id='a', kind='capture', payment_id='p1', amount_cents=1000))
        view = apply_event(store, dict(event_id='e2', order_id='a', kind='refund', payment_id='p1', refund_id='r1', amount_cents=200))
        self.assertEqual(view['status'], 'partially_refunded')
        self.assertEqual(view['refunded_cents'], 200)

    def test_full_refund(self):
        store = new_store()
        apply_event(store, dict(event_id='e1', order_id='a', kind='capture', payment_id='p1', amount_cents=1000))
        view = apply_event(store, dict(event_id='e2', order_id='a', kind='refund', payment_id='p1', refund_id='r1', amount_cents=1000))
        self.assertEqual(view['status'], 'refunded')


if __name__ == '__main__':
    unittest.main(verbosity=2)
