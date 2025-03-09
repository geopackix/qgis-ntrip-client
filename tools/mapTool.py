from qgis.PyQt.QtCore import QObject
from qgis.core import QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem
from qgis.gui import QgsMapToolEmitPoint

class MapTool(QObject):
    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.map_tool = QgsMapToolEmitPoint(self.canvas)
        self.map_tool.canvasClicked.connect(self.on_canvas_clicked)
        self.canvas.setMapTool(self.map_tool)

    def on_canvas_clicked(self, point):
        crs_src = self.canvas.mapSettings().destinationCrs()
        crs_dest = QgsCoordinateReferenceSystem(4326)  # WGS 84
        transform = QgsCoordinateTransform(crs_src, crs_dest, QgsProject.instance())
        lat_lon = transform.transform(point)
        print(f"Latitude: {lat_lon.y()}, Longitude: {lat_lon.x()}")

