# -*- coding: utf-8 -*-
"""
/***************************************************************************
 Move
                                 A QGIS plugin
 The Move plugin links MobilityDB to QGIS to visualize moving objects
                              -------------------
        begin                : 2026-10-09
        git sha              : $Format:%H$
        copyright            : (C) 2026 by MobilityDB
        email                : maxime.schoemans@ulb.be
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/

A map canvas item drawing the temporal points of a temporal point view
directly with QPainter, the Fast preview mode of the plugin.

The item reads the materialized view #create_temporal_view builds for a
temporal point column, one row per piece of a sequence: its line with M the
seconds since the start of the piece, its start and end timestamps and the
inclusivity of its bounds, and for a tcbuffer the line of its radius; it
connects to the database as #refresh does. On each frame of the temporal
controller, positions computes the point of every piece as the geometry
generator of the layer of the same view does in #add_tpoint_layer, with
NumPy over all the pieces at once, and paint draws the points without a map
render job. The item has no layer in the layer tree, so it offers no
identify, selection, attribute table or print layout.
"""

import numpy as np
import psycopg

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsGeometry, QgsProject)
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QDateTime, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPen


# A 2D point in WKB, as the members of a MultiPoint
POINT_WKB = np.dtype([('order', 'u1'), ('type', '<u4'), ('x', '<f8'), ('y', '<f8')])
POINT_COLOR = QColor(255, 60, 60, 200)
POINT_RADIUS_PX = 3


def line_vertices(wkb):
    """The x, y and m arrays of a LineStringM in ISO WKB."""
    data = bytes(wkb)
    order = '<' if data[0] == 1 else '>'
    gtype = int(np.frombuffer(data, dtype=order + 'u4', count=1, offset=1)[0])
    if gtype != 2002:
        raise ValueError(f"expected a LineStringM (WKB type 2002), got {gtype}")
    coords = np.frombuffer(data, dtype=order + 'f8', offset=9).reshape(-1, 3)
    return coords[:, 0], coords[:, 1], coords[:, 2]


def msecs(timestamp):
    """The milliseconds since the epoch of a timestamp without time zone, read
    as a local date and time as QGIS reads the fields of the view."""
    return QDateTime(timestamp).toMSecsSinceEpoch()


class MoveTrajectoryItem(QgsMapCanvasItem):

    def __init__(self, canvas, db, schema, view_name, srid, circles):
        """
        :param canvas: the map canvas the item draws on
        :param db: dict with host/port/database/username/password
        :param schema: the schema of the view
        :param view_name: the temporal point view to read
        :param srid: the SRID of the geometries of the view
        :raises NotImplementedError: for a tcbuffer view in another CRS than
            the canvas
        :param circles: True for a tcbuffer view, whose rows carry a radius
        """
        super(MoveTrajectoryItem, self).__init__(canvas)
        self._canvas = canvas
        self._view_name = view_name
        self._crs = QgsCoordinateReferenceSystem(f"EPSG:{srid}")
        self._circles = circles
        self._load(db, schema, view_name)
        self._project()
        self._drawable = True
        self._canvas.temporalRangeChanged.connect(self.update)
        self._canvas.extentsChanged.connect(self.update)
        self._canvas.destinationCrsChanged.connect(self._on_crs_changed)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _load(self, db, schema, view_name):
        radius = (", st_asbinary(st_geomfromtext(move_radius))"
                  if self._circles else "")
        sql = (f'select st_asbinary(move_geom), move_start_t, move_end_t, '
               f'move_lower_inc, move_upper_inc{radius} '
               f'from "{schema}"."{view_name}"')
        with psycopg.connect(
                host=db['host'],
                port=db['port'],
                dbname=db['database'],
                user=db['username'],
                password=db['password']) as conn:
            rows = conn.execute(sql).fetchall()

        xs, ys, ms, rs, counts = [], [], [], [], []
        start, end, lower, upper = [], [], [], []
        for row in rows:
            x, y, m = line_vertices(row[0])
            xs.append(x)
            ys.append(y)
            ms.append(m)
            counts.append(len(x))
            if self._circles:
                rs.append(line_vertices(row[5])[0])
            start.append(msecs(row[1]))
            end.append(msecs(row[2]))
            lower.append(row[3])
            upper.append(row[4])

        n = len(counts)
        self._count = n
        self._start = np.asarray(start, dtype=np.int64)
        self._end = np.asarray(end, dtype=np.int64)
        self._lower = np.asarray(lower, dtype=bool)
        self._upper = np.asarray(upper, dtype=bool)
        self._first = np.zeros(n + 1, dtype=np.int64)
        np.cumsum(counts, out=self._first[1:])
        empty = np.empty(0)
        self._x = np.concatenate(xs) if n else empty
        self._y = np.concatenate(ys) if n else empty
        # M in microseconds, the resolution of the timestamps of the view
        self._m = (np.rint(np.concatenate(ms) * 1e6).astype(np.int64)
                   if n else np.empty(0, dtype=np.int64))
        self._r = np.concatenate(rs) if (n and self._circles) else empty
        # Search key of a vertex: the index of its piece times a span longer
        # than every piece, plus its M, increasing over all the vertices
        piece = np.repeat(np.arange(n, dtype=np.int64), counts)
        self._span = int(self._m.max()) + 1 if n else 1
        self._key = piece * self._span + self._m

    # ------------------------------------------------------------------
    # Coordinates in the CRS of the canvas
    # ------------------------------------------------------------------
    def _project(self):
        """Set the transform of the points computed in the CRS of the data to
        the CRS of the canvas, None when the two are the same. A layer
        computes the point in the CRS of its data and the map transforms
        it, which is what the transform of the drawn points repeats."""
        dest = self._canvas.mapSettings().destinationCrs()
        if not dest.isValid() or not self._crs.isValid() or dest == self._crs:
            self._transform = None
            return
        if self._circles:
            raise NotImplementedError(
                "Fast preview draws a tcbuffer only in the CRS of its data")
        self._transform = QgsCoordinateTransform(
            self._crs, dest, QgsProject.instance())

    def _on_crs_changed(self):
        try:
            self._project()
            self._drawable = True
        except NotImplementedError:
            self._drawable = False
        self.update()

    def _to_canvas(self, x, y):
        """Transform points to the CRS of the canvas as one MultiPoint."""
        if self._transform is None or len(x) == 0:
            return x, y
        points = np.empty(len(x), dtype=POINT_WKB)
        points['order'] = 1
        points['type'] = 1
        points['x'] = x
        points['y'] = y
        wkb = (b'\x01' + np.uint32(4).tobytes() + np.uint32(len(x)).tobytes()
               + points.tobytes())
        geom = QgsGeometry()
        geom.fromWkb(wkb)
        geom.transform(self._transform)
        out = np.frombuffer(bytes(geom.asWkb()), dtype=POINT_WKB, offset=9)
        return out['x'].copy(), out['y'].copy()

    # ------------------------------------------------------------------
    # Positions at a frame
    # ------------------------------------------------------------------
    def positions(self, frame_start, frame_end):
        """The points drawn for the frame from frame_start to frame_end
        (milliseconds since the epoch): x and y in the CRS of the canvas,
        and the radius of each circle for a tcbuffer, as the geometry
        generator of the layer of the view computes them."""
        none = (np.empty(0), np.empty(0), np.empty(0))
        if self._count == 0 or not self._drawable:
            return none
        # The pieces the temporal filter of the layer keeps for the frame
        rows = np.nonzero((self._start <= frame_end)
                          & (self._end >= frame_start))[0]
        t = frame_end - self._start[rows]
        d = self._end[rows] - self._start[rows]
        instant = d == 0
        inside = ~instant & ((t > 0) | (self._lower[rows] & (t == 0))) \
            & ((t < d) | (self._upper[rows] & (t == d)))
        # An instant is drawn at its point while the frame contains it
        first = self._first[rows[instant]]
        x = [self._x[first]]
        y = [self._y[first]]
        r = [self._r[first]] if self._circles else []
        # A moving piece is interpolated by M at the end of the frame
        mv = rows[inside]
        q = t[inside] * 1000
        i = np.searchsorted(self._key, mv * self._span + q, side='right') - 1
        i = np.clip(i, self._first[mv], self._first[mv + 1] - 2)
        m0 = self._m[i]
        f = (q - m0) / (self._m[i + 1] - m0)
        x.append(self._x[i] + f * (self._x[i + 1] - self._x[i]))
        y.append(self._y[i] + f * (self._y[i + 1] - self._y[i]))
        if self._circles:
            r.append(self._r[i] + f * (self._r[i + 1] - self._r[i]))
        x, y = self._to_canvas(np.concatenate(x), np.concatenate(y))
        return (x, y, np.concatenate(r) if self._circles else np.empty(0))

    # ------------------------------------------------------------------
    # QGraphicsItem
    # ------------------------------------------------------------------
    def updatePosition(self):
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        s = self._canvas.size()
        return QRectF(0, 0, s.width(), s.height())

    def paint(self, painter, option, widget):
        trange = self._canvas.temporalRange()
        if not trange.begin().isValid() or not trange.end().isValid():
            return
        x, y, r = self.positions(trange.begin().toMSecsSinceEpoch(),
                                 trange.end().toMSecsSinceEpoch())
        if len(x) == 0:
            return
        # The affine map from map coordinates to pixels of the canvas
        m2p = self._canvas.getCoordinateTransform()
        c = self._canvas.extent().center()
        o = m2p.transform(c.x(), c.y())
        ex = m2p.transform(c.x() + 1, c.y())
        ey = m2p.transform(c.x(), c.y() + 1)
        dx, dy = x - c.x(), y - c.y()
        px = o.x() + dx * (ex.x() - o.x()) + dy * (ey.x() - o.x())
        py = o.y() + dx * (ex.y() - o.y()) + dy * (ey.y() - o.y())

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QBrush(POINT_COLOR))
        painter.setPen(QPen(Qt.NoPen))
        if self._circles:
            radii = r / m2p.mapUnitsPerPixel()
        else:
            radii = np.full(len(px), POINT_RADIUS_PX, dtype=float)
        for cx, cy, rad in zip(px.tolist(), py.tolist(), radii.tolist()):
            painter.drawEllipse(QPointF(cx, cy), rad, rad)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def cleanup(self):
        for signal, slot in ((self._canvas.temporalRangeChanged, self.update),
                             (self._canvas.extentsChanged, self.update),
                             (self._canvas.destinationCrsChanged,
                              self._on_crs_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        scene = self._canvas.scene()
        if scene is not None and self.scene() is scene:
            scene.removeItem(self)

    def piece_count(self):
        return self._count

    def view_name(self):
        return self._view_name
