"""The trail must hold exactly what left, never a value, and say what it can't vouch for."""
import json
import stat
from pathlib import Path

import pytest
from conftest import SECRET

from gipper import config, custody, ledger
from gipper.ssh import Remote

SCAN_LIMIT = custody.SCAN_MAX_BYTES
FAKE_HF = "hf_" + "a1b2c3d4" * 5


class Local(Remote):
    """A Remote whose 'ssh' is a local shell: the real tar pipeline, no network."""
    def _base(self):
        return ["bash", "-c"]


def tree(tmp_path):
    src = tmp_path / "bundle"
    (src / "sub").mkdir(parents=True)
    (src / "model.py").write_text("print('hi')\n")
    (src / "sub" / "skipme.log").write_text("noise\n")
    (src / ".env.example").write_text("API_KEY=\n")
    return src


def receipts(kind):
    return [r for r in ledger.read(kinds={kind})]


def text_of_trail():
    home = config.DATA
    return "\n".join(p.read_text() for p in (home / "receipts").glob("*.jsonl"))


def test_manifest_is_receipted_before_bytes_move_and_matches_what_arrived(tmp_path):
    src, dst = tree(tmp_path), tmp_path / "there"
    r = Local("h", 22)
    custody.attach(r, "vast", "123", "j1", _job(tmp_path))
    r.put(src, str(dst), exclude=["*.log"])
    up = receipts("custody.upload")[0]["payload"]
    assert up["pod"] == "123" and up["host"] == "h" and up["files"] == 2
    entries = custody.load_manifest(up["manifest"])["entries"]
    paths = {e["path"] for e in entries if e["type"] == "file"}
    assert paths == {"model.py", ".env.example"}   # the exclude applied exactly as on the wire
    assert paths == {str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file()}
    assert receipts("custody.uploaded")[0]["payload"]["ok"] is True
    assert up["findings"] == []                    # a placeholder example file isn't a finding


def test_a_failed_upload_keeps_the_receipt_because_some_bytes_may_have_left(tmp_path):
    class Broken(Remote):
        def _base(self):
            return ["bash", "-c", "cat >/dev/null; exit 3", "x"]
    with pytest.raises(RuntimeError):
        Broken("h", 22).put(tree(tmp_path), "/nowhere")
    assert len(receipts("custody.upload")) == 1
    assert receipts("custody.uploaded")[0]["payload"]["ok"] is False


def test_no_manifest_no_upload(tmp_path):
    sent = tmp_path / "sent"
    with pytest.raises(custody.CustodyError):
        with custody.upload(Local("h", 22), "x", "y", tar_cmd=["tar", "cf", "-", "-C", str(tmp_path), "absent"]):
            sent.write_text("should not run")
    assert not sent.exists() and not receipts("custody.upload")


def test_sensitive_material_is_named_but_its_content_never_recorded(tmp_path, capsys):
    src = tree(tmp_path)
    (src / ".env").write_text("X=1\n")
    (src / "cfg.py").write_text(f"token = '{FAKE_HF}'\n")
    (src / "id_ed25519").write_text("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc\n")
    Local("h", 22).put(src, str(tmp_path / "there"))
    found = {(f["path"], f["rule"]) for f in receipts("custody.upload")[0]["payload"]["findings"]}
    assert {(".env", "sensitive-name"), ("cfg.py", "huggingface-token"), ("id_ed25519", "sensitive-name"), ("id_ed25519", "private-key")} <= found
    assert FAKE_HF not in text_of_trail() and "sensitive finding" in capsys.readouterr().out


def test_a_known_secret_is_refused_before_anything_is_sent(tmp_path):
    src, dst = tree(tmp_path), tmp_path / "there"
    (src / "oops.txt").write_text(f"key={SECRET}\n")
    with pytest.raises(custody.Refused, match="RUNPOD_API_KEY"):
        Local("h", 22).put(src, str(dst))
    assert not dst.exists() and receipts("custody.upload")[0]["payload"]["refused"]
    assert not receipts("custody.uploaded") and SECRET not in text_of_trail()


def test_configured_secret_files_are_refused_even_if_allowed(tmp_path, monkeypatch):
    env = tmp_path / "phone.env"
    env.write_text("PHONE_AUTH=value-that-is-long-enough\n")
    config.CONFIG_FILE.write_text(f'custody_secret_files = ["{env}"]\n')
    src = tree(tmp_path)
    (src / "copy.txt").write_text("value-that-is-long-enough")
    with pytest.raises(custody.Refused, match="PHONE_AUTH"):
        Local("h", 22).put(src, str(tmp_path / "there"))
    marked, why = custody.judge([{"path": "copy.txt", "rule": "known-secret:PHONE_AUTH"}], {"on_sensitive": "warn", "allow": ["copy.txt"]})
    assert why and marked[0]["allowed"]


def test_secret_files_count_only_secret_looking_names_or_the_ones_named(tmp_path):
    env, keys = tmp_path / "app.env", tmp_path / "keys.json"
    token = "-".join(["tok", "abcdefgh", "123"])   # assembled so secret scanners don't flag the fixture
    env.write_text(f"SERVICE_TOKEN={token}\nQUEUE_DB=/home/me/queue.sqlite\nPUBLIC_URL=https://example.org/x\n"
                   "SOMM_KEY_PROFILE=personal-default\nAUTH_TOKEN_FILE=/run/secrets/token\n")
    keys.write_text(json.dumps({"operator": "op-value-1234567", "viewer": "vw-value-1234567", "label": "a public label"}))
    config.CONFIG_FILE.write_text(f'custody_secret_files = ["{env}", {{ path = "{keys}", names = ["operator", "viewer"] }}, "{tmp_path / 'gone.env'}"]\n')
    known = custody.known_secrets()
    assert set(known.values()) >= {"SERVICE_TOKEN", "operator", "viewer"}
    assert "personal-default" not in known and "/run/secrets/token" not in known
    assert "/home/me/queue.sqlite" not in known and "https://example.org/x" not in known and "a public label" not in known


def test_refuse_policy_and_reviewed_allowances(tmp_path):
    finding = [{"path": "fixtures/test.pem", "rule": "sensitive-name"}]
    assert custody.judge(finding, {"on_sensitive": "warn", "allow": []})[1] is None
    assert custody.judge(finding, {"on_sensitive": "refuse", "allow": []})[1]
    marked, why = custody.judge(finding, {"on_sensitive": "refuse", "allow": ["fixtures/*"]})
    assert why is None and marked[0]["allowed"]


def test_check_job_refuses_before_renting(tmp_path):
    src = tree(tmp_path)
    (src / ".env").write_text("X=1\n")
    job = _job(tmp_path, inputs=[src], custody_rules={"on_sensitive": "refuse"})
    with pytest.raises(custody.Refused, match="before renting"):
        custody.check_job(job)


def test_put_secrets_records_names_never_values_and_check_recognises_them(tmp_path):
    r = Local("h", 22)
    custody.attach(r, "runpod", "abc", "j1", _job(tmp_path))
    dst = tmp_path / "there" / "phone.env"
    r.put_secrets({"TWILIO_AUTH_TOKEN": "tok'en value"}, str(dst))
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600 and "TWILIO_AUTH_TOKEN=" in dst.read_text()
    assert "tok'en value" not in text_of_trail()
    old, new = tmp_path / "old.env", tmp_path / "new.env"
    old.write_text('TWILIO_AUTH_TOKEN="tok\'en value"\n')
    new.write_text("TWILIO_AUTH_TOKEN=rotated-value\n")
    assert custody.check(old)[0]["exposed_on"] == ["runpod:abc"]
    assert custody.check(new)[0]["exposed_on"] == []
    rep = custody.report()
    assert rep["rotate"][0]["name"] == "TWILIO_AUTH_TOKEN" and rep["rotate"][0]["machines"] == ["runpod:abc"]


def test_a_failed_secret_write_still_counts_as_exposed(tmp_path):
    class Broken(Remote):
        def _base(self):
            return ["bash", "-c", "cat >/dev/null; exit 3", "x"]
    r = Broken("h", 22)
    custody.attach(r, "runpod", "p9", "j1", _job(tmp_path))
    with pytest.raises(RuntimeError) as e:
        r.put_secrets({"A_TOKEN": "sensitive-value-here"}, "/root/a.env")
    assert "sensitive-value-here" not in str(e.value)
    assert custody.report()["rotate"][0]["machines"] == ["runpod:p9"]


def test_manifest_of_a_large_tree_survives_tar_padding(tmp_path):
    """tar pads to a record boundary after the end marker; closing early would SIGPIPE it."""
    src = tmp_path / "many"
    src.mkdir()
    for i in range(300):
        (src / f"f{i}.txt").write_bytes(b"x" * (i * 37 % 4096))
    (src / "big.bin").write_bytes(b"\0" * (SCAN_LIMIT + 1))
    manifest, _ = custody.take_manifest(["tar", "cf", "-", "-C", str(src), "."], {})
    assert manifest["files"] == 301 and manifest["content_not_scanned"] == 1


def test_fingerprints_are_keyed_not_plain_hashes():
    import hashlib
    assert custody.fingerprint("hunter2") != hashlib.sha256(b"hunter2").hexdigest()[:32]
    assert stat.S_IMODE((config.DATA / "custody" / "salt").stat().st_mode) == 0o600


def test_declaring_what_went_around_gipper_for_a_machine_it_never_rented(tmp_path, capsys):
    env, payload = tmp_path / "s.env", tmp_path / "payload.txt"
    env.write_text("KEY_A=aaaaaaaaaaaa\nKEY_B=bbbbbbbbbbbb\n")
    payload.write_text("data\n")
    custody.declare(provider="runpod", pod="console1", secrets=["KEY_A"], from_env=env, path="/root/x.env", how="scp",
                    at="2026-10-29T21:28:00Z", confidence="inferred")
    custody.declare(provider="runpod", pod="console1", files=[payload], path="/root/payload.txt", how="scp")
    custody.declare(provider="runpod", pod="console1", tunnel="reverse tunnel to local port 8917")
    with pytest.raises(SystemExit, match="KEY_C"):
        custody.declare(provider="runpod", pod="console1", secrets=["KEY_C"], from_env=env)
    rep = custody.report(since="2026-10-01", until="2026-12-01")
    assert rep["rotate"][0]["confidence"] == ["inferred"] and len(rep["machines"]) == 1
    assert rep["machines"][0]["notes"][0]["tunnel"]
    assert custody.find(str(payload))[0]["machine"] == "runpod:console1"
    assert custody.find("payload.txt")[0]["dst"] == "/root/payload.txt"
    custody.show(rep)
    out = capsys.readouterr().out
    assert "aaaaaaaaaaaa" not in out and "KEY_A" in out


def test_old_rentals_and_unmatched_addresses_show_as_gaps_not_as_clean(tmp_path):
    ledger.write("pod.create", {"pod": "old1", "gpu": "RTX 4090"}, job="j0", refs={"name": "pre-trail"})   # no provider: Runpod's
    Local("9.9.9.9", 2222).put(tree(tmp_path), str(tmp_path / "there"))
    gaps = "\n".join(custody.report()["gaps"])
    assert "runpod:old1: rented with no custody trail" in gaps
    assert "no rental in the ledger matches" in gaps


def test_client_remote_is_attributed_by_address(tmp_path):
    r = Local("1.2.3.4", 2200)
    custody.attach(r, "runpod", "p1", "j1", _job(tmp_path))
    Local("1.2.3.4", 2200).put(tree(tmp_path), str(tmp_path / "there"))
    m = custody.report(pod="p1")["machines"][0]
    assert len(m["uploads"]) == 1 and m["uploads"][0]["ok"] is True


def test_machine_records_env_names_not_values(tmp_path):
    job = _job(tmp_path, env={"HF_HOME": "/root/private-path"})
    custody.attach(Local("h", 22), "runpod", "p2", "j1", job)
    p = receipts("custody.machine")[0]["payload"]
    assert p["env_names"] == ["HF_HOME"] and "private-path" not in json.dumps(p) and p["job_sha256"]


def test_providers_bills_reveal_machines_the_ledger_never_saw(monkeypatch):
    class Billing:
        def has_key(self):
            return True
        def billed(self, since):
            return {"known": (1.0, {}), "stranger": (2.0, {})}
    class Failing(Billing):
        def billed(self, since):
            raise RuntimeError(f"HTTP 500 with {SECRET}")
    monkeypatch.setattr("gipper.providers.load", lambda n: {"runpod": Billing(), "vast": Failing()}.get(n) or pytest.skip())
    ledger.write("pod.create", {"provider": "runpod", "pod": "known"}, job="j")
    gaps = custody._unknown_to_ledger(None, {("runpod", "known")}, "runpod")
    assert gaps == ["runpod:stranger: billed by the provider but unknown to gipper: declare what reached it"]
    failed = custody._unknown_to_ledger(None, set(), "vast")
    assert "could not read bills" in failed[0] and SECRET not in failed[0]   # one provider's failure is a gap, not a crash


def test_report_filters_by_window(tmp_path):
    custody.declare(provider="vast", pod="v1", tunnel="t", at="2026-01-01T00:00:00Z")
    custody.declare(provider="vast", pod="v2", tunnel="t", at="2026-06-01T00:00:00Z")
    pods = [m["pod"] for m in custody.report(since="2026-05-01")["machines"]]
    assert pods == ["v2"]


def _job(tmp_path, inputs=(), env=None, custody_rules=None):
    from gipper.job import Job, Transfer
    jf = Path(tmp_path) / "job.toml"
    jf.write_text("name='x'\n")
    return Job(name="x", workload="w", command="true", path=jf, env=env or {}, custody=custody_rules or {},
               inputs=[Transfer(str(i), "d") for i in inputs])
