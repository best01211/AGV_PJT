"""기록된 실측 JSONL로 지도를 오프라인 생성합니다. 현재 시각 대신 기록 시각으로 검증합니다."""
import argparse
import json
from pathlib import Path

from mapping.models import MapConfig, ObservationBatch
from mapping.occupancy_grid import OccupancyGrid
from mapping.map_storage import MapStorage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', default='data/maps')
    args = parser.parse_args()
    grid = OccupancyGrid(MapConfig.model_validate_json(Path(args.config).read_text(encoding='utf-8')))
    with open(args.input, encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            batch = ObservationBatch.model_validate_json(line)
            for measurement in batch.measurements:
                measurement.source = 'recorded'
            result = grid.integrate(batch, now=batch.pose.timestamp_s)
            print(json.dumps({'line': line_number, **result}, ensure_ascii=False))
    map_id = MapStorage(args.output_dir).save(grid)
    print(json.dumps({'map_id': map_id, 'accepted': grid.accepted, 'rejected': grid.rejected}))


if __name__ == '__main__':
    main()
