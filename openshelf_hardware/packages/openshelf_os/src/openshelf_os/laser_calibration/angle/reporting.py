from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .models import StageStallProbeSample
from .status import get_current_timestamp


def write_stage_stall_output_document(
	*,
	output_path: Path,
	metadata: dict[str, Any],
	samples: list[StageStallProbeSample],
	angle_summaries: list[dict[str, Any]],
	best_summary: dict[str, Any] | None,
) -> None:
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(
		json.dumps(
			{
				'created_at': get_current_timestamp(),
				'mode': 'stage-stall',
				'metadata': metadata,
				'samples': [asdict(sample) for sample in samples],
				'angle_summaries': angle_summaries,
				'best_summary': best_summary,
			},
			indent='\t',
		),
		encoding='utf-8',
	)
