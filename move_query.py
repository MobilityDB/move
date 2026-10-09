import psycopg
import uuid


# The layer type of a PostGIS geometry type, None when no layer shows it
def geom_type_family(geom_type):
    geom_type = geom_type.lower()
    if geom_type in ['point', 'multipoint']:
        return 'multipoint'
    if geom_type in ['linestring', 'multilinestring']:
        return 'multilinestring'
    if geom_type in ['polygon', 'multipolygon']:
        return 'multipolygon'
    return None


# Temporal types drawn as a moving point, with the cast to tgeompoint that
# gives their coordinates
TPOINT_CASTS = {
    'tgeompoint': '',
    'tgeogpoint': '::tgeompoint',
    'tnpoint': '::tgeompoint',
}
# Temporal type drawn as a moving circle
TCIRCLE_TYPES = ['tcbuffer']
# Temporal types drawn as the geometry they hold, with the cast to geometry
# of their values
TGEOM_CASTS = {
    'tgeometry': '',
    'tgeography': '::geometry',
}
TEMPORAL_TYPES = list(TPOINT_CASTS) + TCIRCLE_TYPES + list(TGEOM_CASTS)
# Number of segments of a linear sequence held by one feature of a temporal
# point layer, which bounds both the number of features of the layer and the
# number of vertices the map interpolates for each of them in a frame
TPOINT_CHUNK_SEGMENTS = 32


class MoveQuery:
    def __init__(self, raw_sql):
        super(MoveQuery, self).__init__()
        # Short enough for the views and their indexes to keep their names
        # within the 63 bytes of a PostgreSQL identifier
        self.id = uuid.uuid4().hex[:12]
        self.raw_sql = raw_sql
        self.is_valid = True
        self.returned_no_rows = False
        self.parse_raw_query()

    # Parses the query into 7 parts:
    # 1: with_sql (optional)
    # 2: select_sql ("select")
    # 3: full_columns_sql (to parse later)
    # 4: from_sql ("from")
    # 5: rest_sql (what comes after from)
    # 6: limit_sql ("limit", optional)
    # 7: value_sql (limit value, optional)
    def parse_raw_query(self):
        sql = " ".join(self.raw_sql.split()).replace(";", "")
        sql = sql.lower()
        n = sql.count("select")
        i = 0
        prev_b, prev_s, prev_e = "", "", sql
        while i < n:
            b, s, e = prev_e.partition("select")
            b = prev_b + prev_s + b
            if b.count("(") == b.count(")") and e.count("(") == e.count(")"):
                break
            i += 1
            prev_b, prev_s, prev_e = b, s, e
        if i == n:
            self.is_valid = False
            return
        self.has_with = bool(b)
        self.with_sql = b.strip()
        self.select_sql = s.strip()
        columns, from_sql, rest = e.partition("from")
        self.full_columns_sql = columns.strip()
        self.parse_columns()
        self.from_sql = from_sql.strip()
        rest, limit, value = rest.partition("limit")
        self.rest_sql = rest.strip()
        self.has_limit = bool(limit)
        self.limit_sql = limit.strip()
        self.value_sql = value.strip()
        if self.has_limit and not self.value_sql.isnumeric():
            self.is_valid = False

    def parse_columns(self):
        columns_sql = self.full_columns_sql
        columns = []
        next_column = ""
        while True:
            a, b, columns_sql = columns_sql.partition(",")
            next_column += a
            if next_column.count("(") == next_column.count(")"):
                columns.append(next_column.strip())
                next_column = ""
            else:
                next_column += b
            if not columns_sql:
                break
        if next_column:
            self.is_valid = False
            return
        self.columns_sql = columns
        self.columns_parse()

    def columns_parse(self):
        columns = self.columns_sql
        names = []
        functions = []
        for col in columns:
            rest, _, name = col.partition("as")
            functions.append(rest.strip())
            if not name:
                name, _, rest = rest.partition("(")
                if not rest:
                    rest, _, name = name.partition(".")
                    if not name:
                        name = rest
            if name.strip() == "*":
                self.is_valid = False
                return
            names.append(name.strip())
        self.column_functions = functions
        self.column_names = names

    def resolve_types(self, db):
        sql = self.get_typeof_sql()
        types = None
        with psycopg.connect(
                host=db['host'],
                port=db['port'],
                dbname=db['database'],
                user=db['username'],
                password=db['password']) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(sql)
                    types = list(cur.fetchone())
                except psycopg.Error as e:
                    self.error_msg = e.diag.message_primary
                except TypeError:
                    self.error_msg = "Query returned 0 tuples"
                    self.returned_no_rows = True
                conn.commit()
        if types is not None:
            self.column_types = types
            return True
        return False

    def get_column_ids_by_type(self, types, inclusive=True):
        if isinstance(types, str):
            types = [types]
        ids = []
        for i, t in enumerate(self.column_types):
            if ((inclusive and t in types)
                    or (not inclusive and t not in types)):
                ids.append(i)
        return ids

    def geom_cols(self):
        return self.get_column_ids_by_type(['geometry', 'geography'])

    def temp_cols(self):
        return self.get_column_ids_by_type(TEMPORAL_TYPES)

    def other_cols(self):
        return self.get_column_ids_by_type(
            ['geometry', 'geography'] + TEMPORAL_TYPES, False)

    def has_geom_columns(self):
        return len(self.geom_cols()) > 0

    def has_temp_columns(self):
        return len(self.temp_cols()) > 0

    def create_geom_view(self, project_id, db):
        select_sql = self.get_geom_select_sql()
        view_name = f"move_{project_id}_geom_{self.id}"
        sql = f"create materialized view {view_name} as ({select_sql})"
        analyze_sql = f"analyze {view_name}"
        geom_cols = self.geom_cols()
        col_names = [self.column_names[col] for col in geom_cols]
        srids = []
        geom_types = []
        with psycopg.connect(
                host=db['host'],
                port=db['port'],
                dbname=db['database'],
                user=db['username'],
                password=db['password']) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                # The view is created in the current schema of the connection
                cur.execute("select current_schema()")
                schema = cur.fetchone()[0]
                cur.execute(analyze_sql)
                for col_name in col_names:
                    sql = f"select distinct st_srid({col_name}), geometrytype({col_name}) from {view_name} where {col_name} is not null"
                    cur.execute(sql)
                    res = cur.fetchall()
                    col_srids = set()
                    col_geom_types = set()
                    for srid, geom_type in res:
                        col_srids.add(srid)
                        family = geom_type_family(geom_type)
                        if family:
                            col_geom_types.add(family)
                    if len(col_srids) > 1:
                        raise ValueError(f"Geometry column {col_name} has multiple SRIDS: {str(col_srids)}")
                    elif len(col_geom_types) == 0:
                        raise ValueError(f"No supported geometry types in geometry column {col_name}")
                    srids.append(col_srids.pop())
                    geom_types.append(col_geom_types)
                conn.commit()
        return schema, view_name, col_names, srids, geom_types

    def create_temporal_view(self, project_id, db, col_id):
        if self.column_types[col_id] in TGEOM_CASTS:
            select_sql = self.get_tgeom_select_sql(col_id)
            view_name = f"move_{project_id}_tgeom_{str(col_id)}_{self.id}"
        else:
            select_sql = self.get_tpoint_select_sql(col_id)
            view_name = f"move_{project_id}_tpoint_{str(col_id)}_{self.id}"
        sql = f"create materialized view {view_name} as ({select_sql})"
        col_name = self.column_names[col_id]
        srid_sql = f"select st_srid(move_geom) from {view_name} limit 1"
        types_sql = f"select distinct geometrytype(move_geom) from {view_name}"
        analyze_sql = f"analyze {view_name}"
        startt_idx_sql = f"create index {view_name}_startt_idx on {view_name} (move_start_t)"
        endt_idx_sql = f"create index {view_name}_endt_idx on {view_name} (move_end_t)"
        geom_idx_sql = f"create index {view_name}_geom_idx on {view_name} using spgist (move_geom)"
        srid = None
        geom_types = set()
        with psycopg.connect(
                host=db['host'],
                port=db['port'],
                dbname=db['database'],
                user=db['username'],
                password=db['password']) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                # The view is created in the current schema of the connection
                cur.execute("select current_schema()")
                schema = cur.fetchone()[0]
                cur.execute(srid_sql)
                srid = cur.fetchone()[0]
                cur.execute(types_sql)
                for geom_type, in cur.fetchall():
                    family = geom_type_family(geom_type)
                    if family:
                        geom_types.add(family)
                cur.execute(analyze_sql)
                cur.execute(startt_idx_sql)
                cur.execute(endt_idx_sql)
                cur.execute(geom_idx_sql)
                conn.commit()
        return schema, view_name, srid, geom_types

    def get_full_sql(self):
        sql_parts = []
        if self.has_with:
            sql_parts.append(self.with_sql)
        sql_parts.append(self.select_sql)
        sql_parts.append(", ".join(self.columns_sql))
        sql_parts.append(self.from_sql)
        sql_parts.append(self.rest_sql)
        if self.has_limit:
            sql_parts.append(self.limit_sql)
            sql_parts.append(self.value_sql)
        return " ".join(sql_parts)

    def get_typeof_sql(self):
        sql_parts = []
        if self.has_with:
            sql_parts.append(self.with_sql)
        sql_parts.append(self.select_sql)
        typeof_columns = [f"pg_typeof({col})" for col in self.column_functions]
        sql_parts.append(", ".join(typeof_columns))
        sql_parts.append(self.from_sql)
        sql_parts.append(self.rest_sql)
        sql_parts.append("limit 1")
        return " ".join(sql_parts)

    def get_geom_select_sql(self):
        sql_parts = []
        if self.has_with:
            sql_parts.append(self.with_sql)
        sql_parts.append(self.select_sql)
        cols = ['row_number() over () as move_id']
        cols.extend([
            col for i, col in enumerate(self.columns_sql)
            if i in self.other_cols() or i in self.geom_cols()
        ])
        sql_parts.append(", ".join(cols))
        sql_parts.append(self.from_sql)
        sql_parts.append(self.rest_sql)
        if self.has_limit:
            sql_parts.append(self.limit_sql)
            sql_parts.append(self.value_sql)
        return " ".join(sql_parts)

    def get_tpoint_select_sql(self, col_id):
        sql_parts = []
        if self.has_with:
            sql_parts.append(self.with_sql)
        sql_parts.append(self.select_sql)
        inner_cols = [
            col for i, col in enumerate(self.columns_sql)
            if i in self.other_cols() or i == col_id
        ]
        sql_parts.append(", ".join(inner_cols))
        sql_parts.append(self.from_sql)
        sql_parts.append(self.rest_sql)
        if self.has_limit:
            sql_parts.append(self.limit_sql)
            sql_parts.append(self.value_sql)
        inner_sql = " ".join(sql_parts)
        cols = [
            col for i, col in enumerate(self.column_names)
            if i in self.other_cols()
        ]
        cols = ", ".join(cols)
        if cols:
            # add trailing comma if we have additional colums to fetch
            cols = cols + ", "
        # The coordinates are read from tgeompoint values, and from the center
        # and radius of tcbuffer values
        col_type = self.column_types[col_id]
        cast = TPOINT_CASTS.get(col_type, "")
        if col_type in TCIRCLE_TYPES:
            point = "point(getValue(move_inst))"
            radius_col = """
            st_astext(st_makeline(case when cardinality(move_radii) = 1
                then move_radii || move_radii else move_radii end)) as move_radius,"""
            radius_agg = """,
                    array_agg(st_makepointm(radius(getValue(move_inst)), 0, move_m)
                        order by move_m) as move_radii"""
            radius_sel = ", move_radii"
        else:
            point = "getValue(move_inst)"
            radius_col = ""
            radius_agg = ""
            radius_sel = ""

        # One row per piece of each sequence: a chunk of at most
        # TPOINT_CHUNK_SEGMENTS segments of a linear sequence, a segment of a
        # step sequence, or an instant. Its geometry is the line through the
        # instants of the piece whose M is the number of seconds since the
        # start of the piece, and a chunk includes the timestamp it shares
        # with the next chunk only as the first one of the next chunk
        n = TPOINT_CHUNK_SEGMENTS
        sql = f"""
        with temp_1 as (
            {inner_sql}
        ), temp_2 as (
            select {cols}
                shiftTime({self.column_names[col_id]},
                    localtime - (current_time at time zone 'utc')::time){cast} as move_tpoint
            from temp_1
        ), temp_3 as (
            select {cols}
                unnest(case
                    when tempSubtype(move_tpoint) = 'Instant' then array[move_tpoint]
                    when interp(move_tpoint) = 'Discrete' then segments(move_tpoint)
                    else sequences(move_tpoint) end) as move_seq
            from temp_2
        ), temp_4 as (
            select {cols}
                move_seq as move_piece,
                true as move_lower_inc,
                true as move_upper_inc
            from temp_3
            where numInstants(move_seq) = 1
            union all
            select {cols}
                move_piece,
                lowerInc(move_piece::tstzspan),
                upperInc(move_piece::tstzspan)
            from temp_3, unnest(segments(move_seq)) as move_piece
            where numInstants(move_seq) > 1 and interp(move_seq) = 'Step'
            union all
            select {cols}
                atTime(move_seq, span(timestampN(move_seq, {n} * move_k + 1),
                    timestampN(move_seq, least({n} * move_k + {n + 1}, move_n)),
                    true, true)),
                move_k > 0 or lowerInc(move_seq::tstzspan),
                move_k = (move_n - 2) / {n} and upperInc(move_seq::tstzspan)
            from temp_3, numInstants(move_seq) as move_n,
                generate_series(0, (move_n - 2) / {n}) as move_k
            where move_n > 1 and interp(move_seq) <> 'Step'
        ), temp_5 as (
            select {cols}
                move_piece, move_lower_inc, move_upper_inc, move_points{radius_sel}
            from temp_4, lateral (
                select
                    array_agg(st_makepointm(st_x({point}), st_y({point}), move_m)
                        order by move_m) as move_points{radius_agg}
                from unnest(instants(move_piece)) as move_inst,
                    extract(epoch from getTimestamp(move_inst)
                        - startTimestamp(move_piece)) as move_m
            ) as move_vertices
        )
        select
            row_number() over () as move_id,
            {cols}
            st_setsrid(st_makeline(case when cardinality(move_points) = 1
                then move_points || move_points else move_points end),
                SRID(move_piece)) as move_geom,{radius_col}
            startTimestamp(move_piece) at time zone 'gmt' as move_start_t,
            endTimestamp(move_piece) at time zone 'gmt' as move_end_t,
            move_lower_inc,
            move_upper_inc
        from temp_5"""
        return sql

    def get_tgeom_select_sql(self, col_id):
        sql_parts = []
        if self.has_with:
            sql_parts.append(self.with_sql)
        sql_parts.append(self.select_sql)
        inner_cols = [
            col for i, col in enumerate(self.columns_sql)
            if i in self.other_cols() or i == col_id
        ]
        sql_parts.append(", ".join(inner_cols))
        sql_parts.append(self.from_sql)
        sql_parts.append(self.rest_sql)
        if self.has_limit:
            sql_parts.append(self.limit_sql)
            sql_parts.append(self.value_sql)
        inner_sql = " ".join(sql_parts)
        cols = [
            col for i, col in enumerate(self.column_names)
            if i in self.other_cols()
        ]
        cols = ", ".join(cols)
        if cols:
            # add trailing comma if we have additional colums to fetch
            cols = cols + ", "

        # One row per segment, holding the value of the segment from move_start_t
        # to move_end_t; an instant is a segment with equal start and end timestamps
        sql = f"""
        with tracks as (
            {inner_sql}
        ), segs as (
            select {cols}
                unnest(case when tempSubtype(move_tgeom) = 'Instant'
                    then array[move_tgeom] else segments(move_tgeom) end) as move_seg
            from (
                select {cols}
                    shiftTime({self.column_names[col_id]},
                        localtime - (current_time at time zone 'utc')::time) as move_tgeom
                from tracks
            ) shifted
        )
        select
            row_number() over () as move_id,
            {cols}
            startValue(move_seg){TGEOM_CASTS[self.column_types[col_id]]} as move_geom,
            startTimestamp(move_seg) at time zone 'gmt' as move_start_t,
            endTimestamp(move_seg) at time zone 'gmt' as move_end_t
        from segs"""
        return sql

    def __str__(self):
        if not self.is_valid:
            return self.raw_sql
        else:
            return self.get_full_sql()