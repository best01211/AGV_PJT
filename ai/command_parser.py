"""자연어를 의도로 변환합니다. 이 모듈은 주행 상태를 변경하지 않습니다."""
import re


START_POINT = '시작점'
MAX_WAIT_SECONDS = 300
SPOKEN_COUNTS = {'한': 1, '두': 2, '세': 3, '네': 4, '다섯': 5}


def normalize_name(name):
    return ' '.join(name.split()).casefold()


def parse_command(text, destinations=None):
    text = text.strip()
    unknown = {'intent': 'unknown', 'destination': None}
    if not text or len(text) > 200:
        return unknown
    text = text.rstrip('.!?').rstrip()
    text = re.sub(r'^(?:로봇아|제이탱크야)[,\s]+', '', text)
    if re.fullmatch(
        r'(?:(?:지금|바로|당장|일단|잠깐|여기서|그\s*자리에서)[,\s]*)*'
        r'(?:그만\s*가|(?:이동|주행)\s*그만|멈춰|멈추(?:세요|자)|'
        r'(?:(?:이동|주행)\s*)?(?:정지|중지|중단|취소)(?:\s*해(?:\s*(?:요|줘(?:요)?|주세요))?|\s*하세요)?)'
        r'(?:\s*(?:줘(?:요)?|주세요|요))?|stop',
        text, re.I,
    ):
        return {'intent': 'stop', 'destination': None}
    if destinations is None and re.fullmatch(r'[A-Z](?:으로|로|에)\s*이동하다\s*멈춰', text, re.I):
        return {'intent': 'stop', 'destination': None}
    if destinations is not None:
        names = {normalize_name(name): name for name in destinations}

        def resolve(place):
            literal = normalize_name(place)
            base = re.sub(r'\s*(?:지점|장소)$', '', literal)
            for candidate in (literal, base):
                if candidate in names:
                    return names[candidate]
                compact = candidate.replace(' ', '')
                matches = [name for key, name in names.items() if key.replace(' ', '') == compact]
                if len(matches) == 1:
                    return matches[0]
                if matches:
                    break
            return place.strip()

        def resolve_stop(place):
            place = place.strip()
            name = resolve(place)
            if name in destinations:
                return name
            without_particle = re.sub(r'\s*(?:으로|에서|까지|[을를로에])$', '', place)
            name = resolve(without_particle)
            if name in destinations:
                return name
            if normalize_name(without_particle).replace(' ', '') in ('시작', START_POINT):
                for alias in ('시작', START_POINT):
                    registered = resolve(alias)
                    if registered in destinations:
                        return registered
                return START_POINT
            return name

        def itinerary(places, single_via=False):
            stops = [resolve_stop(place) for place in places]
            parsed = {'intent': 'move', 'destination': stops[-1]}
            if len(stops) > 1:
                parsed['via'] = stops[0] if single_via and len(stops) == 2 else stops[:-1]
            return parsed

        polite = r'(?:\s*(?:해\s*(?:줘(?:요)?|주세요|줄래(?:요)?|요)?|하라|하세요|부탁(?:해(?:요)?|합니다)?))?'
        go = r'가\s*(?:줘(?:요)?|주세요|줄래(?:요)?|주실래요|라|요|자|봐(?:요)?)?'
        come_back = r'(?:다시\s*)?(?:돌아\s*)?와\s*(?:줘(?:요)?|주세요|줄래(?:요)?|요)?'
        motion = rf'(?:(?:좀|다시)\s+)*(?:(?:이동|방문|복귀){polite}|(?:돌아\s*)?{go}|{come_back})'
        instruction = re.sub(r'^(?:(?:지금|바로|일단|우선|먼저)[,\s]+)+', '', text)
        retarget = re.fullmatch(r'목적지(?:를)?\s+(.+?(?:으로|로))\s*바꿔\s*(?:줘(?:요)?|주세요)?', instruction)
        if retarget:
            return itinerary([retarget[1]])
        timed_visit = re.fullmatch(
            r'(.+?)(?:에서|(?:으로|로|에|\s+)\s*가서|\s*(?:다녀\s*왔다가|갔다가|들[렀렸]다가))\s*'
            r'(\d+|한|두|세|네|다섯)\s*(초|분)\s*(?:(?:간|동안|만)?\s*(?:대기(?:한)?\s*(?:후|뒤)(?:에)?|'
            r'기다린\s*(?:후|뒤)(?:에)?|기다렸다가|쉬고)|(?:후|뒤)(?:에)?)'
            rf'\s*(.+?)\s+{motion}', instruction)
        if timed_visit:
            place, duration, unit, destination = timed_visit.groups()
            seconds = int(duration) if duration.isdecimal() else SPOKEN_COUNTS[duration]
            if unit == '분':
                seconds *= 60
            return {'intent': 'move', 'destination': resolve_stop(destination),
                    'via': resolve_stop(place), 'wait_seconds': seconds}
        sequence = re.fullmatch(r'(.+?)\s*(?:순\s*서\s*대로|순으로|차례대로|차례로)\s*' + motion, instruction)
        if sequence:
            places = re.split(r'\s*,\s*(?:그리고\s+)?|(?:그리고|[와과]|이?랑|하고)\s+', sequence[1])
            return itinerary(places)
        if re.fullmatch(r'충전\s*하러\s*' + go, instruction):
            return itinerary(['충전소'])
        move = re.fullmatch(r'(.+?)\s+' + motion, instruction)
        if not move:
            move = re.fullmatch(r'(.+?(?:으로|까지|[로에을를]))' + motion, instruction)
        if move:
            places = move[1]
            if resolve_stop(places) in destinations:
                return itinerary([places])
            legs = re.split(r'\s*(?:다녀\s*(?:왔다가|오고|와서)|갔다가|갔다\s*(?:와서|오고)|들[렀렸]다가|들렀다|들러서|들르고|들러|거쳐서?|'
                            r'(?:간|들른|방문한)\s*(?:다음|뒤|후)(?:에)?|가고\s*나서|'
                            r'이동하고|방문하고|,?\s*그\s*다음(?:에)?|,\s*그리고)\s*', places)
            if len(legs) > 1:
                legs = [re.sub(r'^먼저\s+|\s+먼저$', '', part) for part in legs]
                return itinerary(legs, single_via=True)
            return itinerary(places.split(','))
        if re.search(r'목적지\s*(?:목록|리스트)|등록된\s*(?:장소|구역|목적지)|어디(?:로|에)\s*(?:갈\s*수|갈\s*곳)|갈\s*수\s*있는\s*(?:장소|구역|곳)', text):
            return {'intent': 'places', 'destination': None}
        estimate = re.fullmatch(
            r'(.+?)(?:까지|으로|에|로|\s+(?=가는\s*데))\s*(?:가려면\s*|가는\s*데\s*)?'
            r'(?:얼마나\s*(?:걸려(?:요)?|걸릴까(?:요)?|멀어(?:요)?)|'
            r'몇\s*(?:초|분)\s*(?:걸려(?:요)?|걸릴까(?:요)?)|거리\s*알려\s*줘|갈\s*수\s*있(?:어(?:요)?|나요))', text)
        if estimate:
            return {'intent': 'estimate', 'destination': resolve(estimate[1])}
        if not text.startswith('왜') and re.search(r'어디(?:로|에)\s*(?:가고|가는)|어디\s*가는|남은|남았|걸려|걸리|경로|도착했|도착은|목적지가', text):
            return {'intent': 'progress', 'destination': None}
    if re.search(r'왜|상태|이유|설명|뭐\s*하고|뭐\s*해|움직이는\s*중|이동\s*중이야|멈춰\s*있', text):
        return {'intent': 'status', 'destination': None}
    if re.search(r'위치|어디', text):
        return {'intent': 'position', 'destination': None}
    targets = set(re.findall(r'(?<![A-Za-z])([A-Z])(?=$|[^A-Za-z])', text.upper()))
    if destinations is None and len(targets) == 1 and re.search(r'이동|가\s*줘|가\s*$|가라|가요|go|move', text, re.I):
        return {'intent': 'move', 'destination': targets.pop()}
    return unknown


def validate_intent(parsed, destinations):
    """향후 LLM 출력에도 동일하게 적용할 허용 목록 검증."""
    if not isinstance(parsed, dict) or set(parsed) not in (
        {'intent', 'destination'}, {'intent', 'destination', 'via'},
        {'intent', 'destination', 'via', 'wait_seconds'},
    ):
        return 'invalid_structure'
    intent, destination = parsed['intent'], parsed['destination']
    if not isinstance(intent, str) or intent not in {'move', 'stop', 'position', 'status', 'places', 'progress', 'estimate'}:
        return 'unknown_intent'
    timed = 'wait_seconds' in parsed
    if timed:
        seconds = parsed['wait_seconds']
        if intent != 'move' or not isinstance(parsed['via'], str):
            return 'invalid_structure'
        if type(seconds) is not int or not 1 <= seconds <= MAX_WAIT_SECONDS:
            return 'invalid_wait'
    if intent in {'move', 'estimate'}:
        if not isinstance(destination, str) or (
            destination not in destinations and not (intent == 'move' and destination == START_POINT)
        ):
            return 'unknown_destination'
        if 'via' in parsed:
            if intent != 'move':
                return 'invalid_structure'
            via = parsed['via']
            stops = [via] if isinstance(via, str) else via
            if not isinstance(stops, list) or not 1 <= len(stops) <= 9:
                return 'invalid_structure'
            if any(not isinstance(stop, str) or
                   (stop not in destinations and stop != START_POINT)
                   for stop in stops):
                return 'unknown_destination'
    elif destination is not None or 'via' in parsed:
        return 'invalid_structure'
    return None
