"""The planner reply is a positional 7-element array, which smaller models fail
to keep lined up with the day list given in prose — the same week came back with
a different set of skipped days per run, twice skipping a day with no events at
all. The prompt now asks for the day in each object; _align_to_days re-orders on
it and strips it, so nothing downstream sees the extra key."""
import meals


def _plan(*names):
    return [{"day": d, "id": n.lower().replace(" ", "-") if n else None, "name": n, "notes": ""}
            for d, n in zip(meals.DAYS7, names)]


def test_reorders_a_shuffled_reply_by_its_day_labels():
    shuffled = _plan(*"Mon Tue Wed Thu Fri Sat Sun".split())
    shuffled[2], shuffled[5] = shuffled[5], shuffled[2]      # model emitted Sat where Wed belongs

    out = meals._align_to_days(shuffled)
    assert [d["name"] for d in out] == "Mon Tue Wed Thu Fri Sat Sun".split()


def test_strips_the_day_key_so_it_never_reaches_the_store():
    out = meals._align_to_days(_plan(*["x"] * 7))
    assert all("day" not in d for d in out)
    assert all(set(d) == {"id", "name", "notes"} for d in out)


def test_falls_back_to_position_without_labels():
    """An older prompt, or a model that ignores the field, behaves as before."""
    unlabelled = [{"id": str(i), "name": f"n{i}", "notes": ""} for i in range(7)]
    assert meals._align_to_days(unlabelled) == unlabelled


def test_pads_a_short_or_partly_labelled_reply_to_seven_days():
    partial = [{"day": "Wednesday", "id": "w", "name": "Wed dish", "notes": ""},
               {"id": "x", "name": "unlabelled", "notes": ""}]

    out = meals._align_to_days(partial)
    assert len(out) == 7
    assert out[2]["name"] == "Wed dish"          # placed by its label
    assert out[6] == {}                          # nothing claimed Sunday


def test_leaves_a_non_list_reply_alone():
    assert meals._align_to_days({"unexpected": True}) == {"unexpected": True}


def test_strips_brackets_the_model_copies_from_the_library_listing():
    """Options are shown as `- [chicken-pie] Chicken pie`; the household's model
    echoes the brackets into the id on every single day, which matches no recipe
    and quietly breaks the link from a planned dinner to its recipe."""
    plan = [{"day": d, "id": "[chicken-pie]", "name": "Chicken pie", "notes": ""}
            for d in meals.DAYS7]

    assert all(d["id"] == "chicken-pie" for d in meals._align_to_days(plan))


def test_leaves_a_clean_or_absent_id_untouched():
    plan = [{"day": d, "id": "chicken-pie", "name": "Chicken pie", "notes": ""} for d in meals.DAYS7]
    plan[3]["id"] = None                                     # a skipped day
    out = meals._align_to_days(plan)
    assert out[0]["id"] == "chicken-pie" and out[3]["id"] is None
