from datetime import timedelta

from saathi.agents import manage, manager
from saathi.db import Booking, Inventory, User, now
from saathi.services import open_case


def test_changed_price_requires_new_booking_approval(s):
	trip = manager.handle(s, "consumer", 1, "Mumbai se Pune kal 1 raat")
	manager.approve(s, trip["approval"]["id"], True)
	b = s.query(Booking).filter_by(user_id=1, status="confirmed").order_by(Booking.id.desc()).first()
	case = open_case(s, "consumer", 1, "C4", "reschedule")
	date = (now().date() + timedelta(days=10)).isoformat()
	result = manage.request_change(s, s.get(User, 1), case, "reschedule", b.id, date)
	quote = result["approval"]
	item = b.items[0] if hasattr(b, "items") else None
	from saathi.db import BookingItem
	item = item or s.query(BookingItem).filter_by(booking_id=b.id).first()
	s.get(Inventory, item.inventory_id).price += 100
	s.commit()
	result = manager.approve(s, quote["id"], True)
	assert "quote changed" in result["reply"].lower()
	assert result["approval"]["payload_hash"] != quote["payload_hash"]
