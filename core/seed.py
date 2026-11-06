"""Reference rows (sports, sources); idempotent.

  python -m core.seed
"""

from sqlalchemy.dialects.postgresql import insert

from core.db import session
from core.models import SOURCES, SPORTS, Source, Sport


def seed() -> None:
    with session() as s:
        for id_, code, name, active in SPORTS:
            s.execute(insert(Sport).values(id=id_, code=code, name=name, active=active)
                      .on_conflict_do_update(index_elements=["id"], set_={"code": code, "name": name,
                                                                          "active": active}))
        for id_, code, name, kind, trust in SOURCES:
            s.execute(insert(Source).values(id=id_, code=code, name=name, kind=kind, trust=trust)
                      .on_conflict_do_update(index_elements=["id"], set_={"code": code, "name": name, "kind": kind,
                                                                          "trust": trust}))


if __name__ == "__main__":
    seed()
    print(f"seeded {len(SPORTS)} sports, {len(SOURCES)} sources")
