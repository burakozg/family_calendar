"""Phase 4: mailsync — offline tests. The ICS builder is verified by parsing
its output back; inbound iMIP handling runs on crafted emails; outbound runs
against mocked CalDAV/SMTP. The live end-to-end table (MAILSYNC_DESIGN.md §13)
requires a real mailbox.org account and is a manual step."""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

import pytest
from icalendar import Calendar, Event as ICalEvent, vCalAddress

import main
import mailsync

ALIAS = "family-cal@test.invalid"


# ── helpers ──────────────────────────────────────────────────────────────────
def make_ics(uid, summary, dtstart, method="REQUEST", sequence=0, rrule=None,
             organizer="alice@example.com", dtend=None):
    cal = Calendar()
    cal.add("prodid", "-//test//")
    cal.add("version", "2.0")
    cal.add("method", method)
    ev = ICalEvent()
    ev.add("uid", uid)
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("summary", summary)
    ev.add("sequence", sequence)
    ev.add("dtstart", dtstart)
    if dtend is not None:
        ev.add("dtend", dtend)
    if rrule:
        ev.add("rrule", rrule)
    ev["organizer"] = vCalAddress(f"mailto:{organizer}")
    cal.add_component(ev)
    return cal.to_ical().decode()


def imip_email(ics_text, to=ALIAS, msgid=None, method="REQUEST", frm="alice@example.com"):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = "Invitation"
    m["Message-ID"] = msgid or f"<{uuid.uuid4().hex}@example.com>"
    m.set_content("You are invited.")
    m.add_alternative(ics_text, subtype="calendar")
    for p in m.walk():
        if p.get_content_type() == "text/calendar":
            p.set_param("method", method)
    return m.as_bytes()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def fresh():
    """Empty events store + fresh sync state, mailsync enabled with one invitee."""
    if mailsync.F_STATE.exists():
        mailsync.F_STATE.unlink()
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": []})
    s = main.read_settings()
    s["mailSync"] = {**mailsync.DEFAULT_MAILSYNC, "enabled": True,
                     "invitees": ["guest@example.com"]}
    main.write(main.F_SETTINGS, s)
    yield


def parsed_vevent(ics_bytes):
    cal = Calendar.from_ical(ics_bytes)
    return cal, next(iter(cal.walk("VEVENT")))


# ── identity ─────────────────────────────────────────────────────────────────
def test_content_hash_tracks_icon_ignores_id():
    # id is not content; icon now is (the emoji is part of the outbound title).
    a = {"id": "x1", "date": "2026-08-01", "who": "family", "label": "A", "icon": "star"}
    b = {"id": "zz", "date": "2026-08-01", "who": "family", "label": "A", "icon": "star"}
    assert mailsync._content_hash(a, "event") == mailsync._content_hash(b, "event")
    assert mailsync._content_hash({**a, "icon": "gym"}, "event") != mailsync._content_hash(a, "event")
    assert mailsync._content_hash({**a, "label": "B"}, "event") != mailsync._content_hash(a, "event")
    assert mailsync._content_hash({**a, "time": "10:00"}, "event") != mailsync._content_hash(a, "event")


# ── ICS builder ──────────────────────────────────────────────────────────────
def test_build_allday_event_caldav_copy_is_plain():
    body = {"date": "2026-08-10", "label": "Picnic"}
    cal, ev = parsed_vevent(mailsync.build_vevent("u1@x", body, "event", sequence=0))
    assert cal.get("METHOD") is None                       # scheduling-free for CalDAV
    assert ev.get("ORGANIZER") is None and ev.get("ATTENDEE") is None
    assert ev.decoded("DTSTART") == date(2026, 8, 10)
    assert ev.decoded("DTEND") == date(2026, 8, 11)        # exclusive


def test_build_timed_event_tzid_plus_one_hour():
    body = {"date": "2026-08-10", "time": "14:30", "label": "Dentist"}
    ics = mailsync.build_vevent("u2@x", body, "event", sequence=0)
    _, ev = parsed_vevent(ics)
    st = ev.decoded("DTSTART")
    assert (st.hour, st.minute) == (14, 30) and st.tzinfo is not None
    assert ev.decoded("DTEND") - st == timedelta(hours=1)
    assert b"TZID=Europe/Stockholm" in ics


def test_build_multiday_event_exclusive_dtend():
    body = {"date": "2026-08-10", "endDate": "2026-08-12", "label": "Trip"}
    _, ev = parsed_vevent(mailsync.build_vevent("u3@x", body, "event", sequence=0))
    assert ev.decoded("DTEND") == date(2026, 8, 13)


def test_build_recurring_legacy_step14_becomes_weekly2():
    body = {"startDate": "2026-08-03", "step": 14, "label": "Bins"}
    _, ev = parsed_vevent(mailsync.build_vevent("u4@x", body, "recurring", sequence=0))
    rr = ev.get("RRULE")
    assert str(rr.get("FREQ")[0]).upper() == "WEEKLY" and int(rr.get("INTERVAL")[0]) == 2


def test_build_recurring_v2_weekly_byday_until():
    body = {"startDate": "2026-08-03", "freq": "weekly", "interval": 1,
            "byday": ["MO", "FR"], "until": "2026-12-01", "label": "Gym"}
    _, ev = parsed_vevent(mailsync.build_vevent("u5@x", body, "recurring", sequence=0))
    rr = ev.get("RRULE")
    assert [str(d) for d in rr.get("BYDAY")] == ["MO", "FR"]
    assert rr.get("UNTIL")[0] == date(2026, 12, 1)


def test_build_email_copy_has_method_organizer_attendees():
    body = {"date": "2026-08-10", "label": "Party"}
    cal, ev = parsed_vevent(mailsync.build_vevent(
        "u6@x", body, "event", sequence=1, method="REQUEST",
        invitees=["a@x.com", "b@y.com"]))
    assert str(cal.get("METHOD")) == "REQUEST"
    assert str(ev.get("ORGANIZER")) == f"mailto:{ALIAS}"
    att = ev.get("ATTENDEE")
    assert len(att) == 2 and str(att[0]) == "mailto:a@x.com"
    assert int(ev.decoded("SEQUENCE")) == 1


# ── inbound ──────────────────────────────────────────────────────────────────
def test_inbound_request_creates_timed_event(fresh):
    ics = make_ics("evt-1@ext", "Dentist",
                   datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc))
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(ics), st))
    evs = main.read_events()["events"]
    assert len(evs) == 1
    assert evs[0]["label"] == "Dentist" and evs[0]["time"] == "15:30"   # UTC+2 in Sept
    assert st["inbound"]["evt-1@ext"]["local_ids"] == [evs[0]["id"]]


def test_inbound_duplicate_msgid_skipped(fresh):
    ics = make_ics("evt-2@ext", "Once", date(2026, 9, 2))
    raw = imip_email(ics, msgid="<dup@example.com>")
    st = mailsync._read_state()
    run(mailsync._process_message(raw, st))
    run(mailsync._process_message(raw, st))
    assert len(main.read_events()["events"]) == 1


def test_inbound_sequence_update_replaces(fresh):
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("evt-3@ext", "Meet", date(2026, 9, 3), sequence=0)), st))
    run(mailsync._process_message(imip_email(
        make_ics("evt-3@ext", "Meet (moved)", date(2026, 9, 4), sequence=1)), st))
    evs = main.read_events()["events"]
    assert len(evs) == 1 and evs[0]["date"] == "2026-09-04"
    # Stale lower sequence is ignored.
    run(mailsync._process_message(imip_email(
        make_ics("evt-3@ext", "Old", date(2026, 9, 1), sequence=0)), st))
    assert main.read_events()["events"][0]["date"] == "2026-09-04"


def test_inbound_cancel_removes(fresh):
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("evt-4@ext", "Gone", date(2026, 9, 5))), st))
    assert len(main.read_events()["events"]) == 1
    run(mailsync._process_message(imip_email(
        make_ics("evt-4@ext", "Gone", date(2026, 9, 5), method="CANCEL"),
        method="CANCEL"), st))
    assert main.read_events()["events"] == []
    assert "evt-4@ext" not in st["inbound"]


def test_inbound_own_organizer_skipped(fresh):
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("evt-5@ext", "Echo", date(2026, 9, 6), organizer=ALIAS)), st))
    assert main.read_events()["events"] == []


def test_inbound_not_addressed_to_alias_ignored(fresh):
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("evt-6@ext", "NotOurs", date(2026, 9, 7)), to="other@test.invalid"), st))
    assert main.read_events()["events"] == [] and st["inbound"] == {}


def test_inbound_weekly_rrule_maps_to_recurring_v2(fresh):
    st = mailsync._read_state()
    ics = make_ics("evt-7@ext", "Training",
                   datetime(2026, 9, 7, 17, 0, tzinfo=timezone.utc),   # a Monday
                   rrule={"freq": "weekly", "byday": ["MO", "TH"], "interval": 1})
    run(mailsync._process_message(imip_email(ics), st))
    rec = main.read_events()["recurring"]
    assert len(rec) == 1
    assert rec[0]["freq"] == "weekly" and rec[0]["byday"] == ["MO", "TH"]
    assert rec[0]["time"] == "19:00"
    assert st["inbound"]["evt-7@ext"]["kind"] == "recurring"


def test_inbound_monthly_rrule_expands(fresh):
    st = mailsync._read_state()
    start = date.today() + timedelta(days=3)
    ics = make_ics("evt-8@ext", "Book club", start,
                   rrule={"freq": "monthly", "interval": 1})
    run(mailsync._process_message(imip_email(ics), st))
    evs = main.read_events()["events"]
    assert 4 <= len(evs) <= 7                       # ~6 months within the 180-day horizon
    assert st["inbound"]["evt-8@ext"]["expanded"] is True
    # Cancel removes every expanded instance.
    run(mailsync._process_message(imip_email(
        make_ics("evt-8@ext", "Book club", start, method="CANCEL"), method="CANCEL"), st))
    assert main.read_events()["events"] == []


def test_inbound_plain_email_ignored(fresh):
    m = EmailMessage()
    m["From"] = "bob@example.com"
    m["To"] = ALIAS
    m["Message-ID"] = "<plain@example.com>"
    m["Subject"] = "hello"
    m.set_content("no calendar here")
    st = mailsync._read_state()
    run(mailsync._process_message(m.as_bytes(), st))
    assert main.read_events()["events"] == []
    assert "<plain@example.com>" in st["imap"]["processed_msgids"]


# ── outbound ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def net(monkeypatch):
    """Mock the network legs; returns a recorder."""
    calls = {"put": [], "delete": [], "mail": []}

    async def fake_put(uid, ics):
        calls["put"].append((uid, ics))
        return True

    async def fake_delete(uid):
        calls["delete"].append(uid)
        return True

    def fake_smtp(to_addrs, subject, body_text, ics_text, method):
        calls["mail"].append({"to": to_addrs, "subject": subject, "method": method,
                              "ics": ics_text})

    monkeypatch.setattr(mailsync, "caldav_put", fake_put)
    monkeypatch.setattr(mailsync, "caldav_delete", fake_delete)
    monkeypatch.setattr(mailsync, "_smtp_send", fake_smtp)
    return calls


def test_outbound_bootstrap_mirrors_without_email(fresh, net):
    run(main._add_event({"date": "2026-10-01", "who": "family", "icon": "star",
                         "label": "PreExisting"}))
    run(mailsync.sync_outbound())
    assert len(net["put"]) == 1                    # mirrored to CalDAV
    assert net["mail"] == []                       # but no backlog invitations
    st = mailsync._read_state()
    assert st["bootstrapped"] and len(st["outbound"]) == 1


def test_outbound_new_edit_delete_lifecycle(fresh, net):
    run(mailsync.sync_outbound())                  # bootstrap on empty store
    # New event → invitation + CalDAV.
    run(main._add_event({"date": "2026-10-02", "who": "family", "icon": "star",
                         "label": "Dinner"}))
    run(mailsync.sync_outbound())
    assert len(net["mail"]) == 1 and net["mail"][0]["method"] == "REQUEST"
    assert net["mail"][0]["subject"].startswith("Invitation:")
    assert net["mail"][0]["to"] == ["guest@example.com"]
    uid = mailsync._read_state()["outbound"][
        main.read_events()["events"][0]["id"]]["uid"]
    assert net["put"][-1][0] == uid

    # Edit → same UID, SEQUENCE 1, "Updated:" subject.
    ev = main.read_events()
    ev["events"][0]["label"] = "Dinner (moved)"
    main.write(main.F_EVENTS, ev)
    run(mailsync.sync_outbound())
    assert net["mail"][-1]["subject"].startswith("Updated:")
    assert "SEQUENCE:1" in net["mail"][-1]["ics"].replace("\r", "")
    assert net["put"][-1][0] == uid                # overwrote the same resource

    # Delete → CANCEL + CalDAV delete + state cleared.
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": []})
    run(mailsync.sync_outbound())
    assert net["mail"][-1]["method"] == "CANCEL"
    assert net["delete"] == [uid]
    assert mailsync._read_state()["outbound"] == {}


def test_outbound_icon_change_sends_update(fresh, net):
    # The icon's emoji is now part of the title, so an icon-only edit must
    # propagate as an update (new SUMMARY) to CalDAV and invitees.
    run(main._add_event({"date": "2026-10-03", "who": "family", "icon": "star",
                         "label": "Cosmetic"}))
    run(mailsync.sync_outbound())                  # bootstrap registers it, no mail
    puts, mails = len(net["put"]), len(net["mail"])
    ev = main.read_events()
    ev["events"][0]["icon"] = "gym"
    main.write(main.F_EVENTS, ev)
    run(mailsync.sync_outbound())
    assert len(net["mail"]) == mails + 1           # update email sent
    assert net["mail"][-1]["subject"].startswith("Updated:")
    assert "🏋 Cosmetic" in net["mail"][-1]["ics"]  # emoji + name in SUMMARY
    assert len(net["put"]) == puts + 1             # CalDAV resource rewritten


def test_outbound_skips_inbound_items(fresh, net):
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("evt-9@ext", "External", date(2026, 10, 4))), st))
    mailsync._write_state(st)
    run(mailsync.sync_outbound())
    assert net["mail"] == [] and net["put"] == []  # inbound items never echo out


def test_invitee_cleaning_drops_own_addresses():
    ms = {"invitees": ["Guest@Example.com", ALIAS, "account@test.invalid",
                       "guest@example.com", "not-an-email"]}
    assert mailsync._clean_invitees(ms) == ["guest@example.com"]


def test_status_route_shape(client):
    s = client.get("/mailsync/status").json()
    assert s["configured"] is True and s["missing_env"] == []
    assert "password" not in str(s).lower()
    assert set(s["counts"]) == {"inbound", "outbound"}


# ── Sender-based attribution (who) ───────────────────────────────────────────
def test_who_from_emails_matches_member_name():
    members = [{"id": "owner", "label": "Alex"}, {"id": "spouse", "label": "Sam"},
               {"id": "family", "label": "Family"}, {"id": "kid", "label": "Kai"}]
    f = mailsync._who_from_emails
    assert f(members, "alex@example.com") == "owner"
    assert f(members, "samantha@example.com") == "spouse"     # 'sam' as substring
    assert f(members, "", "sam.k@work.com") == "spouse"       # organizer arg counts too
    assert f(members, "stranger@nowhere.com") == ""           # nobody matches
    assert f(members, "hello@samsung.com") == ""              # domain ignored (local-part only)


def test_inbound_attributed_by_sender_email(fresh, net):
    s = main.read_settings()
    for m in s["members"]:
        if m["id"] == "owner":  m["label"] = "Alex"
        if m["id"] == "spouse": m["label"] = "Sam"
    main.write(main.F_SETTINGS, s)
    st = mailsync._read_state()

    def deliver(uid, summary, day, frm):
        run(mailsync._process_message(imip_email(
            make_ics(uid, summary, date(2026, 10, day), organizer="planner@work.com"),
            frm=frm), st))
        return main.read_events()["events"][-1]     # events are date-sorted; newest last

    assert deliver("a@x", "Dinner",  5, "Alex Doe <alex@example.com>")["who"] == "owner"
    assert deliver("b@x", "Yoga",    6, "sam.k@example.com")["who"] == "spouse"
    assert deliver("c@x", "Meeting", 7, "stranger@nowhere.com")["who"] == "family"  # default


# ── F2: an invite from a family member is never echoed back out ───────────────
# The invitee "guest@example.com" (from the `fresh` fixture) plays the family
# member A who sends the invitation from their own client.
A = "guest@example.com"


def test_echo_invite_from_invitee_never_sent(fresh, net):
    """Core scenario: A invites the alias; the event lands locally and NO
    outbound email/CalDAV PUT is produced across two cycles. This also exercises
    the hardening — sync_outbound reads state written by _apply_request itself
    (no explicit _write_state here)."""
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("echo-1@ext", "Team dinner", date(2026, 11, 2), organizer=A), frm=A), st))
    assert len(main.read_events()["events"]) == 1
    run(mailsync.sync_outbound())
    run(mailsync.sync_outbound())
    assert net["mail"] == [] and net["put"] == []
    assert main.read_events()["events"][0]["label"] == "Team dinner"
    assert "echo-1@ext" in mailsync._read_state()["inbound"]     # tracked as inbound


def test_sequence_update_keeps_loop_guard(fresh, net):
    """A bumps the SEQUENCE (moves the event). The replacement local_ids are
    recorded on disk and stay excluded from outbound."""
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("echo-2@ext", "Review", date(2026, 11, 3), sequence=0, organizer=A), frm=A), st))
    run(mailsync._process_message(imip_email(
        make_ics("echo-2@ext", "Review (moved)", date(2026, 11, 5), sequence=1, organizer=A), frm=A), st))
    evs = main.read_events()["events"]
    assert len(evs) == 1 and evs[0]["date"] == "2026-11-05"
    disk = mailsync._read_state()["inbound"]["echo-2@ext"]
    assert disk["local_ids"] == [evs[0]["id"]] and disk["sequence"] == 1
    run(mailsync.sync_outbound())
    assert net["mail"] == [] and net["put"] == []


def test_expanded_rrule_all_ids_guarded(fresh, net):
    """An inbound monthly RRULE expands into N one-off events; every id must be
    recorded so none of them syncs out."""
    st = mailsync._read_state()
    start = date.today() + timedelta(days=3)
    run(mailsync._process_message(imip_email(
        make_ics("echo-3@ext", "Monthly sync", start,
                 rrule={"freq": "monthly", "interval": 1}, organizer=A), frm=A), st))
    ids = {e["id"] for e in main.read_events()["events"]}
    guarded = set(mailsync._read_state()["inbound"]["echo-3@ext"]["local_ids"])
    assert ids and ids == guarded
    run(mailsync.sync_outbound())
    assert net["mail"] == [] and net["put"] == []


def test_local_delete_of_inbound_event_sends_no_cancel(fresh, net):
    """Deleting an inbound-created event via the normal delete path emits no
    CANCEL (it was never in st['outbound'])."""
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("echo-4@ext", "Lunch", date(2026, 11, 6), organizer=A), frm=A), st))
    ev_id = main.read_events()["events"][0]["id"]
    run(main._delete_by_id("events", ev_id))
    run(mailsync.sync_outbound())
    assert net["mail"] == [] and net["delete"] == []


def test_local_edit_of_inbound_event_stays_excluded(fresh, net):
    """Documented behavior: editing an inbound-created item locally does not
    propagate back out."""
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("echo-5@ext", "Call", date(2026, 11, 7), organizer=A), frm=A), st))
    ev = main.read_events()
    ev["events"][0]["label"] = "Call (renamed locally)"
    main.write(main.F_EVENTS, ev)
    run(mailsync.sync_outbound())
    assert net["mail"] == [] and net["put"] == []


def test_auto_accept_reply_is_reply_to_organizer_only(fresh, net):
    """With autoAccept on, the only mail is a METHOD:REPLY addressed to the
    organizer alone — never a REQUEST broadcast, and the event still doesn't
    echo out."""
    s = main.read_settings()
    s["mailSync"]["autoAccept"] = True
    main.write(main.F_SETTINGS, s)
    st = mailsync._read_state()
    run(mailsync._process_message(imip_email(
        make_ics("echo-6@ext", "Standup", date(2026, 11, 8), organizer=A), frm=A), st))
    assert len(net["mail"]) == 1
    m = net["mail"][0]
    assert m["method"] == "REPLY" and m["to"] == [A]
    assert "METHOD:REPLY" in m["ics"].replace("\r", "")
    run(mailsync.sync_outbound())
    assert [x for x in net["mail"] if x["method"] == "REQUEST"] == []
    assert net["put"] == []


# ── F6: owner-name suffix on outbound copies ─────────────────────────────────
def test_owner_suffix_resolution():
    members = [{"id": "owner", "label": "Alex"}, {"id": "spouse", "label": "Sam"},
               {"id": "family", "label": "Family"}]
    f = mailsync._owner_suffix
    assert f({"who": "spouse"}, members) == " (Sam)"
    assert f({"who": "owner"}, members) == " (Alex)"
    assert f({"who": "family"}, members) == ""      # whole-family event: no attribution
    assert f({"who": ""}, members) == ""
    assert f({"who": "ghost"}, members) == ""        # unknown id
    assert f({}, members) == ""


def test_build_vevent_summary_carries_owner():
    body = {"date": "2026-08-01", "label": "Dentist", "icon": "doctor", "who": "spouse"}
    _, ev = parsed_vevent(mailsync.build_vevent("u@x", body, "event", owner=" (Sam)"))
    assert str(ev.get("SUMMARY")) == "🏥 Dentist (Sam)"                 # CalDAV copy
    _, ev2 = parsed_vevent(mailsync.build_vevent(
        "u@x", body, "event", method="REQUEST", invitees=["g@x.com"], owner=" (Sam)"))
    assert str(ev2.get("SUMMARY")) == "🏥 Dentist (Sam)"                # emailed copy
    _, ev3 = parsed_vevent(mailsync.build_vevent("u@x", body, "event"))
    assert str(ev3.get("SUMMARY")) == "🏥 Dentist"                      # no owner → bare


def test_content_hash_ignores_owner_suffix():
    # The suffix is a build-time decoration; the hash is over stored body fields,
    # so a member-label rename never changes it (who does, and it's a hash key).
    body = {"date": "2026-08-01", "label": "Dentist", "icon": "doctor", "who": "spouse"}
    assert mailsync._content_hash(body, "event") == mailsync._content_hash(body, "event")
    assert mailsync._content_hash({**body, "who": "owner"}, "event") != \
           mailsync._content_hash(body, "event")


def _icsstr(x):
    return x.decode() if isinstance(x, (bytes, bytearray)) else x


def test_outbound_email_caldav_and_subject_carry_owner(fresh, net):
    s = main.read_settings()
    for m in s["members"]:
        if m["id"] == "spouse":
            m["label"] = "Sam"
    main.write(main.F_SETTINGS, s)
    run(mailsync.sync_outbound())                                       # bootstrap (empty)
    run(main._add_event({"date": "2026-12-01", "who": "spouse", "icon": "doctor", "label": "Dentist"}))
    run(mailsync.sync_outbound())
    m = net["mail"][-1]
    assert m["method"] == "REQUEST"
    assert m["subject"] == "Invitation: 🏥 Dentist (Sam) — 2026-12-01"
    assert "🏥 Dentist (Sam)" in m["ics"]                               # emailed SUMMARY
    assert any("🏥 Dentist (Sam)" in _icsstr(ics) for _, ics in net["put"])   # CalDAV SUMMARY


def test_invite_body_text_carries_owner():
    body = {"date": "2026-12-01", "label": "Dentist", "who": "spouse"}
    assert mailsync._invite_body_text(body, "event", " (Sam)").startswith("Dentist (Sam) on 2026-12-01")
    assert mailsync._invite_body_text(body, "event").startswith("Dentist on 2026-12-01")


def test_outbound_family_event_has_no_suffix(fresh, net):
    run(mailsync.sync_outbound())
    run(main._add_event({"date": "2026-12-02", "who": "family", "icon": "star", "label": "Party"}))
    run(mailsync.sync_outbound())
    m = net["mail"][-1]
    assert m["subject"] == "Invitation: ⭐ Party — 2026-12-02"          # no (Name)
    assert "⭐ Party" in m["ics"] and "Party (" not in m["ics"]
