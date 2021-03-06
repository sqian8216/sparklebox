import os
import json
from pytz import utc
from datetime import datetime
from time import time as _time
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, UnicodeText, LargeBinary
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, aliased, load_only
from sqlalchemy import func
import multiprocessing
from tornado.ioloop import IOLoop
from functools import partial
from collections import defaultdict, namedtuple
import starlight

unknown_gacha_t = namedtuple("unknown_gacha_t", ("name"))

Gap = namedtuple("Gap", ("start", "end"))
class Availability(object):
    """mutable class so we can meld stuff"""
    _TYPE_GACHA = 1
    _TYPE_EVENT = 2
    def __init__(self, type, name, start, end, gaps, limited):
        self.type = type
        self.name = name
        self.start = start
        self.end = end
        self.gaps = gaps
        self.limited = limited

    def __repr__(self):
        return "models.Availability({0})".format(
            ", ".join(repr(getattr(self, x)) for x in ["type", "name", "start", "end", "gaps"])
        )

def combine_availability(l):
    """Take a list of discrete Availability and turn any small lapses <= 3 days
       into a Gap on the parent object.
       Returns in place because of gacha_availability()"""
    if not l:
        return

    new_list = []
    prev = l[0]
    for availability in l[1:]:
        bet = availability.start - prev.end
        # max 3 day gap, and both descriptions must be limited/non-limited
        if bet.seconds > 0 and bet.days <= 3 and prev.limited == availability.limited:
            prev.gaps.append(Gap(prev.end, availability.start))
            prev.end = availability.end
        else:
            new_list.append(prev)
            prev = availability
    new_list.append(prev)
    l[:] = new_list

time = lambda: int(_time())
Base = declarative_base()

def retry(n):
    def _wrapper(f):
        def __wrapper(*args, **kwargs):
            for _ in range(n):
                try:
                    return f(*args, **kwargs)
                except OperationalError as e:
                    continue
        return __wrapper
    return _wrapper

def utext():
    # hack
    if os.getenv("DATABASE_CONNECT").startswith("sqlite:"):
        return UnicodeText()
    else:
        return UnicodeText(collation="utf8_bin")

TABLE_PREFIX = os.getenv("TLE_TABLE_PREFIX", None)
if TABLE_PREFIX is None:
    print("Warning: env variable TLE_TABLE_PREFIX unset. Defaulting to 'ss'. "
        "(Set TLE_TABLE_PREFIX to silence this warning.)")
    TABLE_PREFIX = "ss"

class TranslationEntry(Base):
    __tablename__ = TABLE_PREFIX + "_translation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(utext())
    english = Column(utext())
    submitter = Column(String(50))
    submit_utc = Column(Integer)

    def __repr__(self):
        return "<TL entry {x.id} '{x.english}' by {x.submitter} @{x.submit_utc}>".format(x=self)

class TranslationCache(Base):
    __tablename__ = TABLE_PREFIX + "_translation_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(utext())
    english = Column(utext())

    def __repr__(self):
        return "<TL entry {x.id} '{x.english}'>".format(x=self)

class HistoryEntry(Base):
    __tablename__ = TABLE_PREFIX + "_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    time = Column(Integer)
    payload = Column(LargeBinary)

    def dt_string(self):
        return self.datetime().strftime("%Y-%m-%d")

    def datetime(self):
        return datetime.fromtimestamp(self.time)

    def asdict(self):
        return json.loads(self.payload.decode("ascii"))

class GachaRewardEntry(Base):
    __tablename__ = TABLE_PREFIX + "_gacha_available_ex"

    gacha_id = Column(Integer, primary_key=True, nullable=False, autoincrement=False)
    step_num = Column(Integer)
    reward_id = Column(Integer, primary_key=True, nullable=False, autoincrement=False)
    recommend_order = Column(Integer)
    limited_flag = Column(Integer, primary_key=True, autoincrement=False)

class GachaPresenceEntry(Base):
    __tablename__ = TABLE_PREFIX + "_gacha_contiguous_presence"

    rowid = Column(Integer, primary_key=True)
    card_id = Column(Integer, nullable=False)
    gacha_id_first = Column(Integer, nullable=False)
    gacha_id_last = Column(Integer, nullable=False)
    avail_start = Column(Integer, nullable=False)
    avail_end = Column(Integer, nullable=False)

class TranslationSQL(object):
    def __init__(self, use_satellite, override_url=None):
        self.really_connected = 0
        self.session_nest = []
        self.connect_url = override_url

        self.history_cache = []
        self.history_is_all_loaded = 0
        self.availability_cache = {}
        self.caches_disabled = bool(os.getenv("TLE_DISABLE_CACHES"))
        if self.caches_disabled:
            print("TranslationSQL: no caching")

    def __enter__(self):
        if not self.really_connected:
            conn_s = self.connect_url or os.getenv("DATABASE_CONNECT")
            self.engine = create_engine(conn_s, echo=False,
                connect_args={"ssl": {"dummy": "yes"}})

            try:
                Base.metadata.create_all(self.engine)
            except TypeError:
                self.engine = create_engine(conn_s, echo=False)
                Base.metadata.create_all(self.engine)

            self.Session = sessionmaker(self.engine)
            self.really_connected = 1

        self.session_nest.append(self.Session())
        return self.session_nest[-1]

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value:
            self.session_nest[-1].rollback()
        self.session_nest[-1].close()
        self.session_nest.pop()

    @retry(5)
    def all(self):
        with self as s:
            transient = aliased(TranslationEntry)
            result = s.query(TranslationEntry).filter(
                s.query(func.count(transient.id))
                .filter(transient.submit_utc >= TranslationEntry.submit_utc)
                .filter(transient.key == TranslationEntry.key)
                .order_by(transient.id.desc())
                .correlate(TranslationEntry)
                .as_scalar() == 1).all()
        return result

    @retry(5)
    def delete_all_entries(self):
        with self as s:
            s.query(TranslationEntry).delete()
            s.query(TranslationCache).delete()

    @retry(5)
    def all_for_key(self, key):
        with self as s:
            result = s.query(TranslationEntry).filter(TranslationEntry.key == key).order_by(TranslationEntry.submit_utc).all()
        return result

    @retry(5)
    def translate(self, done, *key):
        with self as s:
            result = s.query(TranslationCache).filter(TranslationCache.key.in_(key)).limit(len(key)).all()
        done(result)

    @retry(5)
    def set_translation(self, key, eng, sender, force_time=None):
        with self as s:
            s.add(TranslationEntry(key=key, english=eng,
                                   submitter=sender, submit_utc=force_time or time()))
            try:
                thing_to_update = s.query(TranslationCache).filter(TranslationCache.key == key).one()
            except NoResultFound:
                thing_to_update = TranslationCache(key=key, english=eng)
            thing_to_update.english = eng
            s.add(thing_to_update)
            s.commit()

    @retry(5)
    def push_history(self, dt, payload):
        with self as s:
            s.add(HistoryEntry(time=dt, payload=payload))
            s.commit()
        self.history_cache = []

    @retry(5)
    def update_caches(self):
        with self as s:
            transient = aliased(TranslationEntry)
            result = s.query(TranslationEntry).filter(
                s.query(func.count(transient.id))
                .filter(transient.submit_utc >= TranslationEntry.submit_utc)
                .filter(transient.key == TranslationEntry.key)
                .order_by(transient.id.desc())
                .correlate(TranslationEntry)
                .as_scalar() == 1).all()
            s.query(TranslationCache).delete()
            cache_ents = [TranslationCache(key=x.key, english=x.english)
                for x in result if x.key != x.english]
            s.add_all(cache_ents)
            s.commit()

    def gen_presence(self, gacha_list):
        # 3, 1 is the regular gacha
        # 3, 3 is the 60-gem daily paid gacha
        # 2, x is the choose-a-ssr ticket gacha
        gacha_list = list(filter(lambda x: x.type == 3 and x.subtype == 1, gacha_list))
        gacha_map = {x.id: x for x in gacha_list}

        def earliest_gacha(gl):
            return min(gl, key=lambda gacha: gacha.start_date if gacha else utc.localize(datetime.max))
        def latest_gacha(gl):
            return max(gl, key=lambda gacha: gacha.end_date if gacha else utc.localize(datetime.min))
        def fromidlist(idl):
            return [gacha_map.get(id) for id in idl]

        gacha_list.sort(key=lambda x: x.start_date)
        prev = gacha_list[0]

        # the db operation is wrapped so we can retry if needed

        self.seed_initial(prev, delete=1)
        print("Primary seeding completed")

        for gacha in gacha_list[1:]:
            if (gacha.start_date - prev.end_date).seconds < 10:
                print(gacha, "is a continuation of", prev)
                self.extend_gacha(prev, gacha)
            else:
                self.seed_initial(gacha)
            prev = gacha

        self.availability_cache = {}
        return

    @retry(5)
    def add_reward_tracking_entries(self, iterator):
        with self as s:
            for ent in iterator:
                s.add(GachaRewardEntry(gacha_id=ent[0], step_num=ent[1], reward_id=ent[2], recommend_order=ent[3], limited_flag=ent[4]))
            s.commit()

    @retry(5)
    def seed_initial(self, prev, delete=0):
        with self as s:
            if delete:
                s.query(GachaPresenceEntry).delete()

            for card, in s.query(GachaRewardEntry.reward_id).filter(GachaRewardEntry.gacha_id == prev.id):
                s.add(GachaPresenceEntry(card_id=card, gacha_id_first=prev.id, gacha_id_last=prev.id,
                    avail_start=prev.start_date.timestamp(), avail_end=prev.end_date.timestamp()))
                print("Seed", prev.id, "having id", card)
            s.commit()

    @retry(5)
    def extend_gacha(self, prev, new):
        print(prev.id, "->", new.id, "!!")
        with self as s:
            extant_ids = s.query(GachaPresenceEntry.card_id).filter(GachaPresenceEntry.gacha_id_last == prev.id).all()
            # sqlalchemy returns 1-tuples so get the ids out before turning it into a set
            extant_ids = set(x[0] for x in extant_ids)

            ng_ids = s.query(GachaRewardEntry.reward_id).filter(GachaRewardEntry.gacha_id == new.id).all()
            ng_ids = set(x[0] for x in ng_ids)

            # cards in both prev and new gachas
            update_ids = extant_ids & ng_ids
            print(update_ids)

            s.query(GachaPresenceEntry).filter(GachaPresenceEntry.card_id.in_(update_ids), GachaPresenceEntry.gacha_id_last == prev.id).update(
                {GachaPresenceEntry.gacha_id_last: new.id,
                 GachaPresenceEntry.avail_end: new.end_date.timestamp()},
            synchronize_session=False)

            # appearances get a new record
            new_ids = ng_ids - extant_ids
            for id in new_ids:
                s.add(GachaPresenceEntry(card_id=id, gacha_id_first=new.id, gacha_id_last=new.id,
                    avail_start=new.start_date.timestamp(), avail_end=new.end_date.timestamp()))
            print(new_ids)
            s.commit()

    def gacha_availability(self, cards, gacha_list):
        if self.caches_disabled:
            return self._gacha_availability(cards, gacha_list)

        ret = {}
        need_fetch = []
        for k in cards:
            pre = self.availability_cache.get(k)
            if pre is None:
                need_fetch.append(k)
            else:
                ret[k] = pre

        if need_fetch:
            fetch = self._gacha_availability(need_fetch, gacha_list)
            ret.update(fetch)
            self.availability_cache.update(fetch)
        return ret

    @retry(5)
    def _gacha_availability(self, cards, gacha_list):
        print("trace _gacha_availability", cards)
        gacha_map = {x.id: x for x in gacha_list}

        ga = defaultdict(lambda: [])
        for k in cards:
            ga[k] # force the empty list to be created and cached

        with self as s:
            ents = s.query(GachaPresenceEntry).filter(GachaPresenceEntry.card_id.in_(cards)).all()
            limflags = s.query(GachaRewardEntry).options(load_only("gacha_id", "reward_id")) \
                .filter(GachaRewardEntry.reward_id.in_(cards), GachaRewardEntry.limited_flag == 1).all()

        limflags = set((x.gacha_id, x.reward_id) for x in limflags)

        def getgacha(gid):
            if gid in gacha_map:
                return gacha_map[gid]
            else:
                return unknown_gacha_t("??? (unknown gacha ID: {0})".format(gid))

        for e in ents:
            if e.gacha_id_first == e.gacha_id_last or getgacha(e.gacha_id_first).name == getgacha(e.gacha_id_last).name:
                name = getgacha(e.gacha_id_first).name
            else:
                name = None

            # FIXME do this better
            if name == "プラチナオーディションガシャ":
                name = None

            ga[e.card_id].append(Availability(Availability._TYPE_GACHA, name,
                utc.localize(datetime.utcfromtimestamp(e.avail_start)), utc.localize(datetime.utcfromtimestamp(e.avail_end)), [],
                (e.gacha_id_first, e.card_id) in limflags))

        [v.sort(key=lambda x: x.start) for v in ga.values()]
        [combine_availability(v) for v in ga.values()]
        return ga

    def get_history(self, nent):
        if self.caches_disabled:
            return list(self._get_history(nent))

        if self.history_is_all_loaded or (nent and nent <= len(self.history_cache)):
            return self.history_cache[:nent]

        self.history_cache = list(self._get_history(nent))
        if not nent:
            self.history_is_all_loaded = 1
        return self.history_cache

    @retry(5)
    def _get_history(self, nent):
        print("trace _get_history")
        with self as s:
            rows = s.query(HistoryEntry).order_by(HistoryEntry.time.desc())

            if nent:
                rows = rows.limit(nent)
            gv = rows.all()
            yield from gv

class TranslationEngine(TranslationSQL):
    def __init__(self, data_source, use_satellite, override_url=None):
        super().__init__(use_satellite, override_url)
        self.dsrc = data_source
        self.cache_id = -1
        self.k2r = {}

    def kill_caches(self, dv):
        self.k2r = {x.kanji: x.conventional for _, x in self.dsrc.data.names.items()}

        self.history_cache = []
        self.history_is_all_loaded = 0

        self.availability_cache = {}

        self.cache_id = dv

    # TODO: remove
    @staticmethod
    def translate_name(kanji):
        return starlight.data.translate_name(kanji)

    def get_history(self, nent):
        if self.cache_id != self.dsrc.data.version:
            self.kill_caches(self.dsrc.data.version)

        return super().get_history(nent)

    def gacha_availability(self, cards, gacha_list):
        if self.cache_id != self.dsrc.data.version:
            self.kill_caches(self.dsrc.data.version)

        return super().gacha_availability(cards, gacha_list)
