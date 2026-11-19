"""외부 센서·위치 추정 파이프라인이 작성한 ObservationBatch JSONL을 API로 전달."""
import argparse
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mapping.models import ObservationBatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='JSONL 파일 경로 또는 표준 입력의 -')
    parser.add_argument('--url', required=True, help='실행 중인 /api/mapping/observations 전체 URL')
    args = parser.parse_args()
    stream = sys.stdin if args.input == '-' else open(args.input, encoding='utf-8')
    try:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            batch = ObservationBatch.model_validate_json(line)
            request = Request(args.url, data=batch.model_dump_json().encode(), headers={'Content-Type': 'application/json'}, method='POST')
            try:
                with urlopen(request, timeout=5) as response:
                    result = json.load(response)
            except HTTPError as error:
                raise RuntimeError(f'HTTP {error.code}: {error.read().decode()}') from error
            print(json.dumps({'line': line_number, **result}, ensure_ascii=False), flush=True)
    finally:
        if stream is not sys.stdin:
            stream.close()


if __name__ == '__main__':
    main()
