import psycopg

from qgis.core import QgsTask


class MoveTask(QgsTask):
    def __init__(self, description, query, project_id, db, finished_fnc,
                 failed_fnc):
        super(MoveTask, self).__init__(description, QgsTask.CanCancel)
        self.query = query
        self.project_id = project_id
        self.db = db
        self.finished_fnc = finished_fnc
        self.failed_fnc = failed_fnc
        self.result_params = None
        self.error_msg = None

    def finished(self, result):
        if result:
            self.finished_fnc(self.db, self.query, self.result_params)
        else:
            self.failed_fnc(self.error_msg)


class MoveGeomTask(MoveTask):
    def __init__(self, description, query, project_id, db, finished_fnc,
                 failed_fnc):
        super(MoveGeomTask, self).__init__(description, query, project_id,
                                           db, finished_fnc, failed_fnc)

    def run(self):
        try:
            schema, view_name, col_names, srids, geom_types = self.query.create_geom_view(
                self.project_id, self.db)
            self.result_params = {
                'schema': schema,
                'view_name': view_name,
                'col_names': col_names,
                'srids': srids,
                'geom_types': geom_types
            }
        except psycopg.Error as e:
            self.error_msg = str(e)
            return False
        except ValueError as e:
            self.error_msg = str(e)
            return False
        return True


class MoveTTask(MoveTask):
    def __init__(self, description, query, project_id, db, col_id,
                 finished_fnc, failed_fnc):
        super(MoveTTask, self).__init__(description, query, project_id, db,
                                        finished_fnc, failed_fnc)
        self.col_id = col_id

    def run(self):
        try:
            schema, view_name, srid, geom_types = self.query.create_temporal_view(
                self.project_id, self.db, self.col_id)
            self.result_params = {
                'schema': schema,
                'col_id': self.col_id,
                'view_name': view_name,
                'srid': srid,
                'geom_types': geom_types
            }
        except psycopg.Error as e:
            self.error_msg = e.diag.message_primary
            return False
        return True