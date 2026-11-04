"""The job file: what's required, what's refused, and that client jobs written for
0.1's placement profiles still load."""
import pytest

from gipper.job import JobError, load

BASE = 'name = "x"\nworkload = "w"\n[run]\ncommand = "true"\n'


def _job(tmp_path, body):
    p = tmp_path / "job.toml"
    p.write_text(body)
    return load(p)


def test_command_is_required(tmp_path):
    with pytest.raises(JobError):
        _job(tmp_path, 'name = "x"\nworkload = "w"\n')


def test_secrets_do_not_belong_in_env(tmp_path):
    with pytest.raises(JobError):
        _job(tmp_path, BASE + '[env]\nHF_TOKEN = "x"\n')


def test_paths_resolve_against_the_job_file(tmp_path):
    (tmp_path / "in").mkdir()
    j = _job(tmp_path, BASE + '[[inputs]]\nsrc = "in"\ndst = "in"\n[[outputs]]\nsrc = "out"\ndst = "got"\n')
    assert j.inputs[0].src == str(tmp_path / "in") and j.outputs[0].dst == str(tmp_path / "got")


def test_regions_ports_and_volume(tmp_path):
    j = _job(tmp_path, BASE + '[gpu]\nregions = ["us", "ca"]\n[pod]\nports = ["5004/udp"]\n')
    assert j.regions == ["US", "CA"] and j.ports == ["5004/udp"] and j.image == ""
    with pytest.raises(JobError):
        _job(tmp_path, BASE + '[pod]\nports = ["udp"]\n')
    with pytest.raises(JobError):   # network volumes are Runpod's
        _job(tmp_path, BASE + '[gpu]\nproviders = ["vast"]\n[pod]\nvolume = "v"\n')


def test_placement_defaults_when_absent(tmp_path):
    p = _job(tmp_path, BASE).placement
    assert p.objective == "auto" and p.compute_s is None and p.probe_transfer_mb == 0


# Representative placement fields from pre-0.2 client jobs
LEGACY_CLIENT_PLACEMENT = '''[placement]
profile = "batch"
objective = "cost"
setup_s = 120
setup_download_gb = 8.5
download_gb = 0.2
compute_s = 550
min_host_download_mbps = 500
min_host_upload_mbps = 100
min_compute_capability = 8.0
min_cuda = 12.8
transfer_efficiency = 0.5
probe_transfer_mb = 2
probe_timeout_s = 120
min_measured_upload_mbps = 1
min_measured_download_mbps = 1
[placement.compute_s_by_gpu]
A40 = 250
'''


def test_client_jobs_from_0_1_still_load(tmp_path):
    p = _job(tmp_path, BASE + LEGACY_CLIENT_PLACEMENT).placement
    assert p.objective == "cost" and p.compute_s_by_gpu == {"A40": 250} and p.min_cuda == 12.8


def test_interactive_profile_loads(tmp_path):
    p = _job(tmp_path, BASE + '[placement]\nprofile = "interactive"\nlatency_target = "interactive-client"\nmax_rtt_ms = 50\n'
                             'preferred_countries = ["ca", "us"]\n[placement.measured_rtt_ms_by_machine]\n"vast:machine:7" = 28\n').placement
    assert p.objective == "latency" and p.preferred_countries == ["CA", "US"]


@pytest.mark.parametrize("bad", ['[placement]\nobjective = "latency"\n', '[placement]\nmax_rtt_ms = 50\n',
                                 '[placement]\nmax_distance_km = 500\n', '[placement]\nnot_a_field = 1\n',
                                 '[placement]\nobjective = "cheap"\n', '[placement]\nprobe_transfer_mb = 500\n',
                                 '[placement]\nmin_measured_upload_mbps = 5\n'])
def test_placement_mistakes_are_refused(tmp_path, bad):
    with pytest.raises(JobError):
        _job(tmp_path, BASE + bad)
