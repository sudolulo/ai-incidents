"""The deterministic pre-filter: what counts as a candidate, and in which shape.

Several of these are regressions for misses in the sweep this tool was extracted from: a scanner
that only matched confessions scored MISS on three real incidents in one day, and a destructive
command that exited 0 was invisible until tool output was read.
"""

from ai_incidents.prefilter import (
    AI_ADMIT, BLAME, SYNTHETIC, USER_SIG, Patterns, behaviour, danger_rank, extract, is_hit,
)
from ai_incidents.sources import Session, ToolCall, ToolResult, Turn


def sess(turns=(), calls=(), results=()):
    return Session([Turn(r, "2026-07-14", t) for r, t in turns],
                   [ToolCall("Bash", c) for c in calls],
                   [ToolResult(e, t) for e, t in results])


# --- signals --------------------------------------------------------------------------------

def test_agent_admissions_match():
    for s in ["Sorry, I made a mistake there", "That was my error.", "I accidentally deleted the branch",
              "the outage was mine", "This leaked the API key", "I had to revert the migration"]:
        assert AI_ADMIT.search(s), s


def test_user_blame_matches():
    for s in ["you deleted my notes", "why did you push to main?", "that's not what I asked", "put it back"]:
        assert USER_SIG.search(s), s


def test_ordinary_text_does_not_match():
    for s in ["I added the endpoint and the tests pass", "Let's refactor the parser next"]:
        assert not AI_ADMIT.search(s) and not USER_SIG.search(s) and not BLAME.search(s)


def test_blame_catches_is_as_well_as_contraction():
    # "this IS a bug in X" is the commoner phrasing and was once missed by a pattern that only
    # accepted "this's"/"that's".
    assert BLAME.search("This is a bug in node_exporter's parser")
    assert BLAME.search("that's a known issue with the upstream library")
    assert BLAME.search("I'll pin it to an older version as a workaround for the regression")


def test_harness_injected_user_turns_are_not_blame():
    for s in ["<system-reminder>why did you ...</system-reminder>",
              "<local-command-caveat>Caveat: roll back</local-command-caveat>",
              "This session is being continued from a previous conversation. You broke x."]:
        assert SYNTHETIC.match(s)
        assert not is_hit("user", s, Patterns())


def test_user_blame_only_counts_from_the_user():
    assert is_hit("user", "you broke the build", Patterns())
    assert not is_hit("assistant", "you broke the build", Patterns())


# --- behaviour ------------------------------------------------------------------------------

def test_destructive_commands_are_collected_and_scratch_is_benign():
    s = sess(calls=["rm -rf /srv/data/uploads", "rm -rf /tmp/build-cache", "rm -rf mut  # scratchpad",
                    "git worktree remove ../wt", "ls -la"])
    danger, _ = behaviour(s, Patterns())
    assert danger == ["Bash: rm -rf /srv/data/uploads"]


def test_file_paths_are_not_commands():
    # A Read or Edit of a file called truncate.py is not a truncate.
    s = Session(calls=[ToolCall("Edit", "")])
    assert behaviour(s, Patterns()) == ([], [])


def test_success_output_warnings_are_evidence():
    out = "\n".join(["warning: adding embedded git repository: rr/a"] * 14)
    _, failures = behaviour(sess(results=[(False, out)]), Patterns())
    assert failures == ["warning: adding embedded git repository: rr/a   (x14)"]


def test_routine_failures_are_ignored_and_real_ones_kept():
    s = sess(results=[
        (True, "grep: foo: No such file or directory"),
        (True, "fatal: not a git repository"),
        (True, 'Traceback (most recent call last):\n  File "<string>", line 1\nKeyError: x'),
        (True, 'Traceback (most recent call last):\n  File "/srv/app/main.py", line 9\nKeyError: x'),
        (True, "You've hit your spend limit"),
    ])
    _, failures = behaviour(s, Patterns())
    assert len(failures) == 2
    assert failures[0].startswith("Traceback (most recent call last): File \"/srv/app/main.py\"")
    assert "spend limit" in failures[1]


def test_danger_rank_puts_history_rewrites_first():
    cmds = ["pkill -f worker", "systemctl stop web", "rm -rf /tmp/x", "rm -rf ./data",
            "git rm -r --cached .", "git reset --hard HEAD~3"]
    ranked = sorted(cmds, key=danger_rank)
    assert ranked[:2] == ["git rm -r --cached .", "git reset --hard HEAD~3"]
    assert ranked[2] == "rm -rf ./data"
    assert ranked[-1] == "rm -rf /tmp/x"


def test_extra_patterns_from_config():
    pats = Patterns.with_extras(destructive=[r"\bterraform\s+destroy\b"], alarm=[r"MyJobOverdue"])
    danger, _ = behaviour(sess(calls=["terraform destroy -auto-approve"]), pats)
    assert danger
    assert is_hit("assistant", "MyJobOverdue fired at 03:00", pats)
    assert not is_hit("assistant", "MyJobOverdue fired at 03:00", Patterns())


# --- extraction shapes ----------------------------------------------------------------------

def test_clean_session_has_nothing_to_judge():
    ex = extract(sess(turns=[("user", "add a test"), ("assistant", "done, tests pass")], calls=["pytest"]),
                 "claude-code", "abcd1234")
    assert ex.clean and not ex.candidates


def test_snippet_carries_surrounding_turns():
    ex = extract(sess(turns=[("user", "clean up the repo"), ("assistant", "I deleted the wrong directory"),
                             ("user", "restore it")]), "claude-code", "abcd1234")
    assert [c.kind for c in ex.candidates] == ["SNIPPET", "SNIPPET"]
    body = ex.candidates[0].body
    assert "[user] clean up the repo" in body and "[assistant] I deleted the wrong directory" in body


def test_digest_when_nobody_says_anything():
    # The incidents nobody narrates: no confession, no complaint, only commands.
    ex = extract(sess(turns=[("user", "tidy the checkout"), ("assistant", "Done.")],
                      calls=["git reset --hard origin/main"]), "claude-code", "abcd1234")
    [c] = ex.candidates
    assert c.kind == "DIGEST"
    assert "[task] tidy the checkout" in c.body
    assert "! Bash: git reset --hard origin/main" in c.body
    assert "[ended] Done." in c.body


def test_a_session_that_talked_is_still_a_session_that_did():
    # A session with confessions about one thing must still have its commands examined.
    ex = extract(sess(turns=[("user", "commit it"), ("assistant", "My mistake, wrong branch name")],
                      calls=["git add -A && git commit -m wip"],
                      results=[(False, "warning: adding embedded git repository: a\n" * 3)]),
                 "claude-code", "abcd1234")
    assert [c.kind for c in ex.candidates] == ["SNIPPET", "EVIDENCE"]
    assert "embedded git repository" in ex.candidates[1].body


def test_evidence_keeps_the_worst_commands_when_capped():
    calls = [f"rm -rf /tmp/scratch-{i} && true" for i in range(10)] + ["git rm -r --cached ."]
    # the /tmp ones are not BENIGN here (not matched by the scratch patterns) so they compete
    calls = [c.replace("/tmp/", "./tmp-") for c in calls]
    ex = extract(sess(turns=[("user", "go")], calls=calls), "claude-code", "abcd1234")
    assert "git rm -r --cached ." in ex.candidates[0].body.splitlines()[2]


def test_excerpts_are_one_line_each():
    ex = extract(sess(turns=[("assistant", "I broke it\n## Fake heading\nmore")]), "claude-code", "abcd1234")
    lines = ex.candidates[0].body.splitlines()
    assert len(lines) == 1 and "## Fake heading" in lines[0]


def test_talkative_session_is_cut_once_and_keeps_its_evidence():
    turns = [("assistant", f"I made a mistake #{i}") for i in range(30)]
    ex = extract(sess(turns=turns, calls=["git push --force origin main"]), "claude-code", "abcd1234",
                 per_session=5)
    assert len(ex.candidates) == 5
    assert ex.candidates[-1].kind == "EVIDENCE"
    assert ex.dropped == 26
    assert "not shown" in ex.candidates[-1].body


def test_candidate_render_has_id_and_shape():
    ex = extract(sess(turns=[("user", "go")], calls=["rm -rf ./build"]), "opencode", "f44d5651")
    c = ex.candidates[0]
    c.id = "C07"
    assert c.render().splitlines()[0] == (
        "[C07] DIGEST · opencode · f44d5651 · 2026-07-14 -- nobody said anything; the commands did")
