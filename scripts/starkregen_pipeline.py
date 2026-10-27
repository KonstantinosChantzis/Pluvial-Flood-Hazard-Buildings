# -*- coding: utf-8 -*-
"""
Pluvial Flood (Starkregen) Hazard Analysis for Buildings (ArcGIS Pro / ArcPy)
===========================================================================

Automates the DGM-based heavy-rain hazard workflow used for Essen-Kupferdreh:
terrain sinks (ponding depth), surface flow paths and a risk class per building.

Pipeline
--------
 1. Mosaic DGM1 tiles (1 m, NRW) into one 32-bit float raster
 2. Fill sinks
 3. Sink depth = filled DGM - original DGM
 4. Flow direction (D8) and flow accumulation
 5. Flow paths: cells with an upstream area >= threshold (default 1,000 m2)
 6. Stream order (Strahler) and conversion to polylines
 7. Buildings (OSM GeoJSON) -> feature class in EPSG:25832
 8. Maximum sink depth per building (zonal statistics)
 9. Highest Strahler order of flow paths touching each building (2 m search radius)
10. Risk class 0-3 per building and summary table (gdb + CSV)

Requirements: ArcGIS Pro 3.x with Spatial Analyst.

Usage (ArcGIS Pro Python window or propy):
    python starkregen_pipeline.py
Adjust the PARAMETERS block below for another area.

Author: Konstantinos Chantzis, 2026 - MIT License
"""

import csv
import glob
import os

import arcpy
from arcpy.sa import Con, Fill, FlowAccumulation, FlowDirection, Minus, StreamOrder, StreamToFeature

# --------------------------------------------------------------------------
# PARAMETERS
# --------------------------------------------------------------------------
PROJECT_DIR = r"C:\Users\user\Desktop\QGIS_Projekte\ArcGIS\7_Starkregen_Essen"
GDB = os.path.join(PROJECT_DIR, "Starkregen_Kupferdreh.gdb")
DATA = os.path.join(PROJECT_DIR, "Data")

DGM_TILES = glob.glob(os.path.join(DATA, "DGM1", "dgm1_32_*.tif"))
BUILDINGS_GEOJSON = os.path.join(DATA, "buildings_kupferdreh.geojson")

SR = arcpy.SpatialReference(25832)      # ETRS89 / UTM zone 32N
CELL_SIZE = 1                           # m
FLOW_THRESHOLD_M2 = 1000                # min. upstream area for a flow path
SEARCH_RADIUS = "2 Meters"              # building <-> flow path

# Risk classes (project-specific thresholds, not an official standard)
#   3 high   : sink depth >= 0.5 m  or Strahler >= 4
#   2 medium : sink depth >= 0.3 m  or Strahler == 3
#   1 low    : sink depth >= 0.1 m  or Strahler 1-2
#   0 none   : otherwise
RISK_CODE = """
def risk(d, s):
    d = d or 0
    s = s or 0
    if d >= 0.5 or s >= 4:
        return 3
    if d >= 0.3 or s == 3:
        return 2
    if d >= 0.1 or s >= 1:
        return 1
    return 0
"""

# --------------------------------------------------------------------------
arcpy.env.overwriteOutput = True
arcpy.CheckOutExtension("Spatial")
if not arcpy.Exists(GDB):
    arcpy.management.CreateFileGDB(PROJECT_DIR, os.path.basename(GDB))
arcpy.env.workspace = GDB
arcpy.env.outputCoordinateSystem = SR


def p(name):
    return os.path.join(GDB, name)


print("1  Mosaic DGM1 tiles ({})".format(len(DGM_TILES)))
arcpy.management.MosaicToNewRaster(
    DGM_TILES, GDB, "DGM1", SR, "32_BIT_FLOAT", CELL_SIZE, 1, "LAST")
dgm = p("DGM1")
arcpy.env.extent = dgm
arcpy.env.snapRaster = dgm

print("2  Fill sinks")
filled = Fill(dgm)
filled.save(p("DGM1_Fill"))

print("3  Sink depth")
sink = Minus(filled, dgm)
sink.save(p("Senkentiefe"))

print("4  Flow direction / accumulation")
fdir = FlowDirection(filled, "FORCE", None, "D8")
fdir.save(p("FlowDir"))
facc = FlowAccumulation(fdir, None, "FLOAT", "D8")
facc.save(p("FlowAcc"))

print("5  Flow paths >= {} m2".format(FLOW_THRESHOLD_M2))
cells = FLOW_THRESHOLD_M2 / float(CELL_SIZE * CELL_SIZE)
streams = Con(facc >= cells, 1)
streams.save(p("Fliesswege_Raster"))

print("6  Strahler order -> polylines")
order = StreamOrder(streams, fdir, "STRAHLER")
order.save(p("StreamOrder"))
StreamToFeature(order, fdir, p("Fliesswege"), "SIMPLIFY")

print("7  Buildings")
arcpy.conversion.JSONToFeatures(BUILDINGS_GEOJSON, p("Gebaeude_WGS84"), "POLYGON")
arcpy.management.Project(p("Gebaeude_WGS84"), p("Gebaeude"), SR)
arcpy.management.Delete(p("Gebaeude_WGS84"))

print("8  Max. sink depth per building")
arcpy.sa.ZonalStatisticsAsTable(
    p("Gebaeude"), "OBJECTID", sink, p("ZS_Gebaeude_Senken"), "DATA", "MAXIMUM")
zs_id = [f.name for f in arcpy.ListFields(p("ZS_Gebaeude_Senken")) if f.name.upper().startswith("OBJECTID")][-1]
arcpy.management.JoinField(p("Gebaeude"), "OBJECTID", p("ZS_Gebaeude_Senken"), zs_id, ["MAX"])

print("9  Flow paths touching buildings")
fm = arcpy.FieldMappings()
fm.addTable(p("Gebaeude"))
fm.addTable(p("Fliesswege"))
idx = fm.findFieldMapIndex("grid_code")
fmap = fm.getFieldMap(idx)
fmap.mergeRule = "Max"
fm.replaceFieldMap(idx, fmap)
arcpy.analysis.SpatialJoin(
    p("Gebaeude"), p("Fliesswege"), p("Gebaeude_Risiko"),
    "JOIN_ONE_TO_ONE", "KEEP_ALL", fm, "INTERSECT", SEARCH_RADIUS)

print("10 Risk class + summary")
arcpy.management.CalculateField(
    p("Gebaeude_Risiko"), "Risiko", "risk(!MAX!, !grid_code!)", "PYTHON3", RISK_CODE, "SHORT")
arcpy.analysis.Statistics(
    p("Gebaeude_Risiko"), p("Stat_Risiko"), [["Shape_Area", "SUM"]], "Risiko")

out_csv = os.path.join(DATA, "Buildings_Risk.csv")
fields = ["OBJECTID", "building", "addr_street", "addr_housenumber", "Shape_Area", "MAX", "grid_code", "Risiko"]
fields = [f for f in fields if f in [x.name for x in arcpy.ListFields(p("Gebaeude_Risiko"))]]
with open(out_csv, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(fields)
    with arcpy.da.SearchCursor(p("Gebaeude_Risiko"), fields) as cur:
        for row in cur:
            w.writerow(row)

with arcpy.da.SearchCursor(p("Stat_Risiko"), ["Risiko", "FREQUENCY", "SUM_Shape_Area"], sql_clause=(None, "ORDER BY Risiko DESC")) as cur:
    for r, n, a in cur:
        print("   Risk {}: {:5d} buildings, {:10,.0f} m2".format(r, n, a))
print("Done ->", GDB)
