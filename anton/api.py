"""``anton.api.invoke`` — the public replay entry point (SPEC §9). The CLI is a thin wrapper.

    from anton.api import invoke
    result = invoke("artifacts/read_savings_balance.json", {"member_id": "204817"})
    print(result.status, result.outputs)
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from anton.replay.engine import ParamError, ReplayOptions, check_outputs_path, run_replay, validate_params
from anton.schema.loader import LoaderError, load_bundle
from anton.schema.result import ReplayResult

__all__ = ["invoke", "ParamError", "LoaderError"]


def invoke(
    artifact: str | Path,
    params: Mapping[str, str],
    *,
    overlay: str | Path | None = None,
    policy: str | Path | None = None,
    url: str | None = None,
    approve_risky: bool = False,
    allow_draft: bool = False,
    escalate: bool = False,
    escalate_timeout_s: int = 600,
    console_port: int = 7071,
    outputs_file: str | Path | None = None,
    record_video: bool = False,
    run_dir: str | Path | None = None,
    headless: bool | None = None,
) -> ReplayResult:
    """Validate params (``ParamError``), load the bundle (``LoaderError``), then run.

    Both exceptions mean "no run directory was created" — the CLI maps them to exit 2. The
    returned result is the redacted view; pass ``outputs_file`` for raw sensitive outputs.
    """
    bundle = load_bundle(artifact, overlay_path=overlay, policy_path=policy, url=url)
    clean = validate_params(bundle.artifact, {k: str(v) for k, v in params.items()})
    if outputs_file is not None:
        outputs_file = check_outputs_path(Path(outputs_file))
    if record_video and bundle.profile.sensitive_elements:
        # Playwright video cannot be masked; it would record the member page in clear under runs/.
        raise ParamError(
            f"--record-video is refused for profile {bundle.profile.app_id!r}: it declares sensitive_elements and video cannot be masked (SPEC §8)"
        )
    opts = ReplayOptions(
        approve_risky=approve_risky,
        allow_draft=allow_draft,
        escalate=escalate,
        escalate_timeout_s=escalate_timeout_s,
        console_port=console_port,
        outputs_file=Path(outputs_file) if outputs_file is not None else None,
        record_video=record_video,
        run_dir=Path(run_dir) if run_dir is not None else None,
        headless=headless,
    )
    return run_replay(bundle, clean, opts)
