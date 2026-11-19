import csv
from pathlib import Path


def load_measurements(path):
    path=Path(path)
    paths=[path]
    if path.name=='measurements.csv':
        for name in ('measurements_v2.csv','measurements_v3.csv','measurements_v4.csv'):
            additional=path.with_name(name)
            if additional.is_file():
                paths.append(additional)
    rows=[]
    for source in paths:
        with source.open(encoding='utf-8-sig',newline='') as stream:
            rows.extend(csv.DictReader(stream))
    if len({row['sample_id'] for row in rows})!=len(rows):
        raise ValueError('Duplicate measurement sample IDs')
    return rows
