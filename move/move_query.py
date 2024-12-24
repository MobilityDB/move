import psycopg
import uuid


class MoveQuery:
    def __init__(self, raw_sql):
        super(MoveQuery, self).__init__()
        self.id = uuid.uuid4().hex
        self.raw_sql = raw_sql
        self.is_valid = True
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
        return self.get_column_ids_by_type(
            ['tgeompoint', 'tgeogpoint', 'tgeometry'])

    def other_cols(self):
        return self.get_column_ids_by_type(
            ['geometry', 'geography', 'tgeompoint', 'tgeogpoint', 'tgeometry'], False)

    def has_geom_columns(self):
        return len(self.geom_cols()) > 0

    def has_temp_columns(self):
        return len(self.temp_cols()) > 0

    def create_geom_view(self, project_title, db):
        select_sql = self.get_geom_select_sql()
        view_name = f"move_{project_title}_geom_{self.id}"
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
                cur.execute(analyze_sql)
                for col_name in col_names:
                    sql = f"select distinct st_srid({col_name}), geometrytype({col_name}) from {view_name} where {col_name} is not null"
                    cur.execute(sql)
                    res = cur.fetchall()
                    col_srids = set()
                    col_geom_types = set()
                    for srid, geom_type in res:
                        col_srids.add(srid)
                        if geom_type.lower() in ['point', 'multipoint']:
                            col_geom_types.add('multipoint')
                        elif geom_type.lower() in ['linestring', 'multilinestring']:
                            col_geom_types.add('multilinestring')
                        elif geom_type.lower() in ['polygon', 'multipolygon']:
                            col_geom_types.add('multipolygon')
                    if len(col_srids) > 1:
                        raise ValueError(f"Geometry column {col_name} has multiple SRIDS: {str(col_srids)}")
                    elif len(col_geom_types) == 0:
                        raise ValueError(f"No supported geometry types in geometry column {col_name}")
                    srids.append(col_srids.pop())
                    geom_types.append(col_geom_types)
                conn.commit()
        return view_name, col_names, srids, geom_types

    def create_temporal_view(self, project_title, db, col_id):
        if self.column_types[col_id] == 'tgeometry':
            select_sql = self.get_tgeom_select_sql(col_id)
            view_name = f"move_{project_title}_tgeom_{str(col_id)}_{self.id}"
        else:
            select_sql = self.get_tpoint_select_sql(col_id)
            view_name = f"move_{project_title}_tpoint_{str(col_id)}_{self.id}"
        sql = f"create materialized view {view_name} as ({select_sql})"
        col_name = self.column_names[col_id]
        srid_sql = f"select st_srid(geom) from {view_name} limit 1"
        analyze_sql = f"analyze {view_name}"
        startt_idx_sql = f"create index {view_name}_startt_idx on {view_name} (start_t)"
        endt_idx_sql = f"create index {view_name}_endt_idx on {view_name} (end_t)"
        geom_idx_sql = f"create index {view_name}_geom_idx on {view_name} using spgist (geom)"
        srid = None
        with psycopg.connect(
                host=db['host'],
                port=db['port'],
                dbname=db['database'],
                user=db['username'],
                password=db['password']) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(srid_sql)
                srid = cur.fetchone()[0]
                cur.execute(analyze_sql)
                cur.execute(startt_idx_sql)
                cur.execute(endt_idx_sql)
                cur.execute(geom_idx_sql)
                conn.commit()
        return view_name, srid

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
        cols = ['row_number() over () as id']
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

        sql = f"""
        with temp_1 as (
            {inner_sql}
        ), temp_2 as (
            select {cols}
                geometry(shiftTime({self.column_names[col_id]},
                    localtime - (current_time at time zone 'utc')::time), false) as geom
            from temp_1
        )
        select 
            row_number() over () as id,
            {cols}
            geom,
            to_timestamp(st_m(st_startpoint(geom))) at time zone 'gmt' as start_t,
            to_timestamp(st_m(st_endpoint(geom))) at time zone 'gmt' as end_t
        from temp_2"""
        return sql

    def get_tgeom_select_sql(self, col_id):
        sql_parts = []
        if self.has_with:
            sql_parts.append(self.with_sql)
        sql_parts.append(self.select_sql)
        inner_cols = ["row_number() over () as tgeom_id"]
        inner_cols.extend([
            col for i, col in enumerate(self.columns_sql)
            if i in self.other_cols() or i == col_id
        ])
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
            sql = f"""
            with tracks as (
                {inner_sql}
            ), insts as (
                select
                    tgeom_id,
                    {cols},
                    unnest(instants(shiftTime({self.column_names[col_id]}, 
                        localtime - (current_time at time zone 'utc')::time))) as inst
                from tracks
            ), pairs as (
                select 
                    row_number() over () as id, 
                    tgeom_id, 
                    {cols}, 
                    getTimestamp(inst) as t, 
                    getValue(inst) as geom 
                from insts
            ) 
            select 
                id,
                {cols}, 
                geom, 
                t at time zone 'gmt' as start_t, 
                lead(t) over (partition by tgeom_id order by t) at time zone 'gmt' as end_t 
            from pairs"""
        else:
            sql = f"""
            with tracks as (
                {inner_sql}
            ), insts as (
                select
                    tgeom_id,
                    unnest(instants(shiftTime({self.column_names[col_id]}, 
                        localtime - (current_time at time zone 'utc')::time))) as inst
                from tracks
            ), pairs as (
                select 
                    row_number() over () as id, 
                    tgeom_id, 
                    getTimestamp(inst) as t, 
                    getValue(inst) as geom 
                from insts
            ) 
            select 
                id,
                geom, 
                t at time zone 'gmt' as start_t, 
                lead(t) over (partition by tgeom_id order by t) at time zone 'gmt' as end_t 
            from pairs"""
        return sql

    def __str__(self):
        if not self.is_valid:
            return self.raw_sql
        else:
            return self.get_full_sql()