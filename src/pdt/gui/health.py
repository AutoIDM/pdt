"""Health grid: runs bucketed onto a period/sub-period grid.

Each granularity pairs an X unit with the sub-unit that becomes Y — hour by
minute, day by hour of day, week by day of week, month by week of month.
Runs are bucketed in the machine's local time, and future cells are
projected from each app's schedule; a paused app projects nothing, because
its schedule does not fire.
"""

from __future__ import annotations

import math
import zoneinfo
from datetime import UTC, date, datetime, time, timedelta

from croniter import croniter
from django.db.models import Q
from tzlocal import get_localzone

from pdt import config
from pdt.gui.models import Run

DISPLAY_TZ = get_localzone()

GUTTER_X = 34
GUTTER_Y = 18

# Colour tracks the share of runs that failed, not the count. An absolute
# threshold only works at day granularity: a week bucket holds ~68 runs and a
# month bucket ~192, so "three failures" would paint those views solid red.
RATE_STEPS = ((0.5, 'hg-bad'), (0.2, 'hg-w2'), (0.0, 'hg-w1'))

# Size shows how much ran, measured against the busiest bucket in the window.
MIN_R = 1.8
MAX_R = 4.4

# Future hours ride the same scale, then shrink: a hollow ring reads heavier
# than a filled dot, and a run that has not happened should not outweigh one
# that has.
SCHED_SCALE = 0.7

# Cron projection stops here regardless of window length. Nobody reads a
# two-month schedule forecast, and month view would otherwise spin croniter
# through tens of thousands of occurrences.
PROJECTION_DAYS = 8
MAX_PROJECTED_RUNS = 20000

DAY_NAMES = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

# lead is how many columns past now the default window reaches, so the right
# edge shows what is coming. Roughly a week everywhere it fits; a week of hour
# columns would bury the history, and a month-ahead forecast would be a mostly
# empty column, so those two are pulled in.
GRANULARITIES = {
    'hour': {'label': 'Hour', 'rows': 12, 'cols': 96, 'col_w': 10, 'row_h': 22, 'lead': 24},
    'day': {'label': 'Day', 'rows': 24, 'cols': 84, 'col_w': 12, 'row_h': 11, 'lead': 7},
    'week': {'label': 'Week', 'rows': 7, 'cols': 78, 'col_w': 12, 'row_h': 26, 'lead': 1},
    'month': {'label': 'Month', 'rows': 5, 'cols': 48, 'col_w': 20, 'row_h': 30, 'lead': 0},
}
DEFAULT_GRANULARITY = 'day'


def build_grid(project, gran=DEFAULT_GRANULARITY, end=None, app_name=None, page=0):
    """Build the grid for one granularity, ending on the given anchor.

    end is a datetime in DISPLAY_TZ naming the last column, and defaults to two
    columns past now so the right edge shows what is coming. page shifts the
    window by thirds, which keeps stepping consistent across granularities.
    """
    if gran not in GRANULARITIES:
        gran = DEFAULT_GRANULARITY
    spec = GRANULARITIES[gran]

    now = datetime.now(UTC).astimezone(DISPLAY_TZ)
    end = _col_start(
        gran, end.astimezone(DISPLAY_TZ) if end else now, 0 if end else spec['lead']
    )
    if page:
        end = _col_start(gran, end, page * (spec['cols'] // 3))
    start = _col_start(gran, end, -(spec['cols'] - 1))
    stop = _col_start(gran, end, 1)

    jobs = []
    job_index = {}
    buckets = {}
    spans = {}

    runs = Run.objects.filter(app__project=project, started__gte=start, started__lt=stop)
    if app_name:
        runs = runs.filter(app__name=app_name)

    for name, status, started_at, ended_at in runs.values_list(
        'app__name', 'status', 'started', 'ended'
    ):
        local = started_at.astimezone(DISPLAY_TZ)
        position = _position(gran, local, start, spec)
        if position is None:
            continue
        fail = None
        if status == 'failed':
            fail = [_job_index(jobs, job_index, name), local.strftime('%H:%M')]
        _tally(buckets, '%d|%d' % position, status, fail)
        if gran == 'hour':
            _carry_hours(buckets, spans, local, ended_at, start, spec, position, status, fail)

    horizon = min(stop, now + timedelta(days=PROJECTION_DAYS))
    if horizon > now:
        _project_scheduled(max(now, start), horizon, gran, start, spec,
                           buckets, jobs, job_index, app_name)

    return {
        'gran': gran,
        'granularities': [
            {'key': key, 'label': value['label'], 'active': key == gran}
            for key, value in GRANULARITIES.items()
        ],
        'width': GUTTER_X + spec['cols'] * spec['col_w'],
        # Hour marks that spill into the next column point past the last row,
        # so the canvas needs a little room below the plot.
        'height': GUTTER_Y + spec['rows'] * spec['row_h'] + (MAX_R if gran == 'hour' else 0),
        'plot_bottom': GUTTER_Y + spec['rows'] * spec['row_h'],
        'plot_width': spec['cols'] * spec['col_w'],
        'plot_height': spec['rows'] * spec['row_h'],
        'cells': _cells(buckets, spans, spec, gran),
        'row_labels': _row_labels(gran, spec),
        'col_marks': _col_marks(gran, start, spec),
        'today_x': _today_x(gran, now, start, spec),
        'today_x2': (_today_x(gran, now, start, spec) or 0) + spec['col_w'],
        'today_w': spec['col_w'],
        'range_label': _range_label(gran, start, end),
        'zone': str(DISPLAY_TZ),
        'end': end.isoformat(),
        'is_latest': _col_start(gran, now, spec['lead']) <= end,
        'app': app_name or '',
        'data': {
            'start': start.isoformat(),
            # Plain calendar parts so the browser can label buckets without
            # reinterpreting an ISO offset in its own timezone.
            'startParts': [start.year, start.month, start.day, start.hour],
            'gran': gran,
            'cols': spec['cols'],
            'rows': spec['rows'],
            'colW': spec['col_w'],
            'rowH': spec['row_h'],
            'gutterX': GUTTER_X,
            'gutterY': GUTTER_Y,
            'jobs': jobs,
            'cells': buckets,
        },
    }


def cell_runs(project, gran, start, col, row, app_name=None):
    """Every run inside one bucket, for the expanded focus card.

    Kept out of the grid payload on purpose: listing all runs up front would
    triple it, for detail that is only ever read one bucket at a time.
    """
    window = bucket_window(gran, start, col, row)
    if window is None:
        return []

    begin, finish = window
    runs = Run.objects.filter(app__project=project, started__lt=finish)
    if gran == 'hour':
        # An hour mark visibly spans the minutes it ran for, so a slice of it
        # answers "what was running then" — including runs that started earlier.
        # Coarser cells mean "runs that started here", matching their counts.
        runs = runs.filter(
            Q(ended__gt=begin) | Q(ended__isnull=True, started__gte=begin)
        )
    else:
        runs = runs.filter(started__gte=begin)
    if app_name:
        runs = runs.filter(app__name=app_name)

    stamp = '{0:%H:%M}' if gran in ('hour', 'day') else '{0:%b} {0.day} {0:%H:%M}'
    return [
        {
            'pk': pk,
            'name': name,
            'status': status,
            'time': stamp.format(started_at.astimezone(DISPLAY_TZ)),
        }
        for pk, name, status, started_at in runs.order_by('started').values_list(
            'pk', 'app__name', 'status', 'started'
        )
    ]


def bucket_window(gran, start, col, row):
    """Absolute [begin, end) covered by one cell."""
    if gran not in GRANULARITIES:
        return None
    spec = GRANULARITIES[gran]
    if not (0 <= col < spec['cols'] and 0 <= row < spec['rows']):
        return None

    column = _col_start(gran, start, col)
    if gran == 'hour':
        begin = column + timedelta(minutes=row * 5)
        return begin, begin + timedelta(minutes=5)
    if gran == 'day':
        begin = datetime.combine(column.date(), time(hour=row), tzinfo=DISPLAY_TZ)
        return begin, begin + timedelta(hours=1)
    if gran == 'week':
        begin = datetime.combine(column.date() + timedelta(days=row), time.min, tzinfo=DISPLAY_TZ)
        return begin, begin + timedelta(days=1)

    # Month rows are seven-day slices; the last one runs to the month's end.
    first = column.date().replace(day=row * 7 + 1)
    begin = datetime.combine(first, time.min, tzinfo=DISPLAY_TZ)
    finish = _col_start('month', column, 1) if row == spec['rows'] - 1 else datetime.combine(
        first + timedelta(days=7), time.min, tzinfo=DISPLAY_TZ
    )
    return begin, finish


def _col_start(gran, moment, offset=0):
    """Start of the column holding moment, shifted by offset columns."""
    if gran == 'hour':
        return moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset)
    if gran == 'day':
        return datetime.combine(moment.date() + timedelta(days=offset), time.min, tzinfo=DISPLAY_TZ)
    if gran == 'week':
        monday = moment.date() - timedelta(days=moment.weekday())
        return datetime.combine(monday + timedelta(weeks=offset), time.min, tzinfo=DISPLAY_TZ)

    total = moment.year * 12 + (moment.month - 1) + offset
    return datetime.combine(date(total // 12, total % 12 + 1, 1), time.min, tzinfo=DISPLAY_TZ)


def _position(gran, local, start, spec):
    """(column, row) for a moment, or None when it falls outside the grid."""
    if gran == 'hour':
        col = int((local - start).total_seconds()) // 3600
        row = local.minute // 5
    elif gran == 'day':
        col = (local.date() - start.date()).days
        row = local.hour
    elif gran == 'week':
        col = ((local.date() - timedelta(days=local.weekday())) - start.date()).days // 7
        row = local.weekday()
    else:
        col = (local.year - start.year) * 12 + local.month - start.month
        row = min((local.day - 1) // 7, spec['rows'] - 1)

    if 0 <= col < spec['cols'] and 0 <= row < spec['rows']:
        return col, row
    return None


def _tally(buckets, key, status, fail, covered=False):
    """Count a run into one bucket. A run is tallied into every bucket it was
    in flight for, so hovering mid-run reports what was actually running.
    """
    cell = buckets.setdefault(key, {'t': 0})
    cell['t'] += 1
    if fail is not None:
        cell.setdefault('f', []).append(fail)
    elif status == 'running':
        cell['r'] = cell.get('r', 0) + 1
    if covered:
        cell['cv'] = True
    return cell


def _carry_hours(buckets, spans, local, ended_at, start, spec, position, status, fail):
    """Record the minutes a run occupied, spilling into the following columns
    when it outlives its hour.

    Two things come out of this. The buckets get a tally for every row the run
    was in flight for, so a hover mid-run reports it. The spans get one interval
    per column, which is what the ribbon is drawn from.
    """
    col, row = position
    column_start = start + timedelta(hours=col)
    begin = (local - column_start).total_seconds() / 60
    finish = begin
    if ended_at:
        finish = (ended_at.astimezone(DISPLAY_TZ) - column_start).total_seconds() / 60

    column = col
    while column < spec['cols']:
        segment_start = begin if column == col else 0.0
        segment_end = min(60.0, finish - (column - col) * 60)
        spans.setdefault(column, []).append({
            'b': segment_start,
            'e': segment_end,
            'failed': fail is not None,
            'ci': column > col,
            'co': finish - (column - col) * 60 > 60,
        })
        _cover(buckets, column, (0 if column > col else row) + 1, segment_end,
               spec, status, fail)
        if finish - (column - col) * 60 <= 60:
            break
        column += 1
        _tally(buckets, '%d|0' % column, status, fail)


def _cover(buckets, col, first_row, last_minute, spec, status, fail):
    last_row = min(int(last_minute) // 5, spec['rows'] - 1)
    for row in range(first_row, last_row + 1):
        _tally(buckets, '%d|%d' % (col, row), status, fail, covered=True)


def _job_index(jobs, job_index, name):
    if name not in job_index:
        job_index[name] = len(jobs)
        jobs.append([name, name])
    return job_index[name]


def scheduled_apps(app_name=None):
    """(name, cron, timezone) for every app whose schedule fires: enabled and not paused."""
    found = []
    for name in config.find_apps():
        if app_name and name != app_name:
            continue
        try:
            app = config.merged_app(name)
            if app['pause'] or app['schedule'] is None:
                continue
            found.append((name, config.cron_expression(app['schedule']), app['timezone']))
        except config.ConfigError:
            continue
    return found


def _project_scheduled(from_dt, to_dt, gran, start, spec, buckets, jobs, job_index,
                       app_name=None):
    """Fill future cells from each app's schedule."""
    projected = 0
    for name, cron, cron_tz in scheduled_apps(app_name):
        try:
            ticks = croniter(cron, from_dt.astimezone(_cron_tz(cron_tz)))
        except (ValueError, KeyError):
            continue

        while projected < MAX_PROJECTED_RUNS:
            occurrence = ticks.get_next(datetime)
            if occurrence >= to_dt:
                break
            position = _position(gran, occurrence.astimezone(DISPLAY_TZ), start, spec)
            projected += 1
            if position is None:
                continue
            cell = buckets.setdefault('%d|%d' % position, {'t': 0})
            cell['s'] = cell.get('s', 0) + 1
            cell.setdefault('sj', []).append(_job_index(jobs, job_index, name))


def _cron_tz(cron_tz):
    """The zone a schedule is read in; `local` (the windows provider) is this machine's."""
    if cron_tz and cron_tz.strip().lower() != 'local':
        try:
            return zoneinfo.ZoneInfo(cron_tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    return DISPLAY_TZ


def _cells(buckets, spans, spec, gran):
    # Covered rows sit underneath a pill drawn from its starting row. They exist
    # for hover only, so they neither draw nor influence the size scale.
    drawn = [(key, cell) for key, cell in buckets.items() if not cell.get('cv')]
    busiest = max(
        (max(cell['t'], cell.get('s', 0)) for _, cell in drawn), default=0
    )

    cells = []
    for key, cell in drawn:
        col, row = (int(part) for part in key.split('|'))
        # Hour runs are drawn as ribbons from the spans, not from these cells.
        if gran == 'hour' and cell['t']:
            continue
        if cell['t']:
            radius = _radius(cell['t'], busiest)
            css_class = _rate_class(len(cell.get('f', ())), cell['t'], cell.get('r'))
        else:
            radius = max(MIN_R, round(_radius(cell['s'], busiest) * SCHED_SCALE, 2))
            css_class = 'hg-sched'

        cy = GUTTER_Y + row * spec['row_h'] + spec['row_h'] / 2
        # Every mark is a pill: corner radius equals the bubble radius, so a
        # run with no measurable duration draws as exactly the same circle.
        cells.append({
            'x': GUTTER_X + col * spec['col_w'] + spec['col_w'] / 2 - radius,
            'y': round(cy - radius, 2),
            'w': radius * 2,
            'h': radius * 2,
            'r': radius,
            'cls': css_class,
        })

    if gran == 'hour':
        cells.extend(_hour_ribbons(spans, spec, busiest))
    return cells


MIN_SPAN_MINUTES = 1.0


def _hour_ribbons(spans, spec, busiest):
    """One ribbon per block of overlapping runs, its width tracking how many
    were in flight at each moment. A single short run comes out as the same
    pill as before; a busy stretch swells and thins along its length.
    """
    minute_px = spec['row_h'] / 5
    marks = []
    for col, intervals in spans.items():
        for block in _blocks(intervals):
            marks.append(_ribbon(col, block, spec, busiest, minute_px))
    return marks


def _blocks(intervals):
    """Group overlapping runs, and within each group step the concurrency."""
    # Round once, up front. Comparing raw bounds against rounded edges drops an
    # interval whose start rounds down, which silently loses its mark.
    bounds = [
        (round(iv['b'], 4), round(max(iv['e'], iv['b'] + MIN_SPAN_MINUTES), 4), iv)
        for iv in intervals
    ]
    edges = sorted({edge for begin, end, _ in bounds for edge in (begin, end)})

    steps = []
    for lower, upper in zip(edges, edges[1:]):
        live = [iv for begin, end, iv in bounds if begin <= lower and end >= upper]
        if live:
            steps.append({'b': lower, 'e': upper, 'live': live})

    blocks = []
    for step in steps:
        if blocks and abs(blocks[-1][-1]['e'] - step['b']) < 1e-6:
            blocks[-1].append(step)
        else:
            blocks.append([step])
    return blocks


def _ribbon(col, block, spec, busiest, minute_px):
    centre = GUTTER_X + col * spec['col_w'] + spec['col_w'] / 2
    top = GUTTER_Y + block[0]['b'] * minute_px
    bottom = GUTTER_Y + block[-1]['e'] * minute_px

    widths = [_radius(len(step['live']), busiest) for step in block]
    notch_top = any(iv['ci'] for iv in block[0]['live'])
    point_bottom = any(iv['co'] for iv in block[-1]['live'])

    runs = {id(iv): iv for step in block for iv in step['live']}.values()
    failed = sum(1 for iv in runs if iv['failed'])

    # Down the right edge, stepping out and in as the count changes.
    right = []
    for index, step in enumerate(block):
        right.append('L%g,%g' % (centre + widths[index], GUTTER_Y + step['b'] * minute_px))
        right.append('L%g,%g' % (centre + widths[index], GUTTER_Y + step['e'] * minute_px))
    left = []
    for index in range(len(block) - 1, -1, -1):
        left.append('L%g,%g' % (centre - widths[index], GUTTER_Y + block[index]['e'] * minute_px))
        left.append('L%g,%g' % (centre - widths[index], GUTTER_Y + block[index]['b'] * minute_px))

    head = _cap(centre, top, widths[0], notch_top, opening=True)
    tail = _cap(centre, bottom, widths[-1], point_bottom, opening=False)
    return {
        'd': ' '.join([head] + right + [tail] + left + ['Z']),
        'cls': _rate_class(failed, len(runs), False),
    }


def _cap(centre, y, half, pointed, opening):
    """Start or finish the outline: rounded, notched inwards, or pointed out."""
    if opening:
        if pointed:
            return 'M%g,%g L%g,%g L%g,%g' % (centre - half, y, centre, y + half, centre + half, y)
        return 'M%g,%g A%g,%g 0 0 1 %g,%g' % (centre - half, y, half, half, centre + half, y)
    if pointed:
        return 'L%g,%g L%g,%g' % (centre, y + half, centre - half, y)
    return 'A%g,%g 0 0 1 %g,%g' % (half, half, centre - half, y)


def _rate_class(failed, total, running):
    if failed:
        return next(c for threshold, c in RATE_STEPS if failed / total > threshold)
    return 'hg-run' if running else 'hg-ok'


def _radius(count, busiest):
    """Area proportional to count, measured against the busiest bucket."""
    if not busiest:
        return MIN_R
    return max(MIN_R, round(MAX_R * math.sqrt(count / busiest), 2))


def _row_labels(gran, spec):
    if gran == 'hour':
        rows = [(row, ':%02d' % (row * 5)) for row in (0, 3, 6, 9)]
    elif gran == 'day':
        rows = [(hour, '%02d' % hour) for hour in (0, 6, 12, 18)]
    elif gran == 'week':
        rows = list(enumerate(DAY_NAMES))
    else:
        rows = [(row, 'W%d' % (row + 1)) for row in range(spec['rows'])]

    return [
        {'text': text, 'y': GUTTER_Y + row * spec['row_h'] + spec['row_h'] / 2}
        for row, text in rows
    ]


def _col_marks(gran, start, spec):
    """Rules with labels, at whatever boundary reads naturally."""
    marks = []
    for col in range(spec['cols']):
        moment = _col_start(gran, start, col)
        if gran == 'hour':
            if moment.hour % 6:
                continue
            label = f'{moment.hour % 12 or 12}{moment:%p}'.lower() if moment.hour else f'{moment:%b} {moment.day}'
        elif gran == 'day':
            if moment.weekday():
                continue
            label = f'{moment:%b} {moment.day}' if moment.day <= 7 else str(moment.day)
        elif gran == 'week':
            monday = moment.date()
            sunday = monday + timedelta(days=6)
            if monday.day == 1:
                first = monday
            elif monday.month != sunday.month:
                first = date(sunday.year, sunday.month, 1)
            else:
                continue
            offset = (first - monday).days
            label = first.strftime('%b') if first.month != 1 else first.strftime('%b %Y')
            marks.append({
                'd': _month_step(col, offset, spec),
                'label_x': GUTTER_X + (col + (1 if offset > 3 else 0)) * spec['col_w'],
                'label': label,
            })
            continue
        else:
            if moment.month != 1 and col:
                continue
            label = moment.strftime('%Y')

        x = GUTTER_X + col * spec['col_w']
        marks.append({'x': x, 'label_x': x, 'label': label})
    return marks


def _month_step(col, weekday, spec):
    """The month boundary as it falls on a calendar: down the right edge of the
    straddling week for the days still in the old month, across at the first of
    the month, then down the left edge for the days already in the new one.
    """
    left = GUTTER_X + col * spec['col_w']
    right = left + spec['col_w']
    step = GUTTER_Y + weekday * spec['row_h']
    bottom = GUTTER_Y + spec['rows'] * spec['row_h']
    return 'M%g,%g L%g,%g L%g,%g L%g,%g' % (
        right, GUTTER_Y, right, step, left, step, left, bottom
    )


def _today_x(gran, now, start, spec):
    """Left edge of the column holding now, for the highlight band."""
    position = _position(gran, now, start, spec)
    if position is None:
        return None
    return GUTTER_X + position[0] * spec['col_w']


def _range_label(gran, start, end):
    if gran == 'hour':
        return '%s – %s' % tuple(f'{t:%b} {t.day}, {t.hour % 12 or 12}{t:%p}' for t in (start, end))
    if gran == 'month':
        return '%s – %s' % (start.strftime('%b %Y'), end.strftime('%b %Y'))
    return '%s – %s' % tuple(f'{t:%b} {t.day}, {t:%Y}' for t in (start, end))
