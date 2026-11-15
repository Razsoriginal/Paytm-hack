import os

os.environ.update(DATABASE_URL="sqlite://", SARVAM_MODE="mock", LLM_MODE="mock",
	COGNEE_MODE="mock", PAYTM_PG_MODE="mock", MOCK_PARTNER_FAILURE_RATE="0")

import pytest  # noqa: E402

from saathi import db, seed  # noqa: E402


@pytest.fixture()
def s():
	db.init_db(drop=True)
	session = db.get_session()
	seed.seed(session)
	yield session
	session.close()
