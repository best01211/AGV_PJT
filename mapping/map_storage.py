import json
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from mapping.occupancy_grid import OccupancyGrid


class MapStorage:
    def __init__(self, directory):
        self.directory = Path(directory)

    def save(self, grid):
        self.directory.mkdir(parents=True, exist_ok=True)
        map_id = uuid4().hex
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, suffix='.tmp')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(grid.export(), stream, allow_nan=False)
            os.replace(temporary, self.directory / f'{map_id}.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return map_id

    def load(self, map_id):
        if not re.fullmatch(r'[a-f0-9]{32}', map_id):
            raise ValueError('invalid map ID')
        with (self.directory / f'{map_id}.json').open(encoding='utf-8') as stream:
            return OccupancyGrid.restore(json.load(stream))
