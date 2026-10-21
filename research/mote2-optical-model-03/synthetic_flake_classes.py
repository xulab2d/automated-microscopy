import cv2
import numpy as np
import shapely.geometry as sg


class FlakeObject:
    def __init__(self, contour, thickness) -> None:
        self.contour = np.array(contour)
        self.thickness = thickness

    def split_flake(self):
        if self.thickness == 1:
            return [self]

        if len(self.contour) <= 8:
            return [self]

        contour_shaply = sg.Polygon(self.contour)

        # get 2 points on the contour
        i, j = np.random.randint(0, len(self.contour), 2)
        if i >= j:
            return [self]
        # check if the line that connects the 2 points is fully inside the contour
        line = sg.LineString([self.contour[i], self.contour[j]])
        try:
            is_intersecting = line.crosses(contour_shaply)
        except:  # noqa
            return [self]

        if is_intersecting:
            return [self]

        # if it is, split the contour into 2 contours
        # for this take all points from i to j for the first contour and all points from j to i for the second contour
        contour1 = []
        contour1.append(self.contour[i])
        contour1.extend(self.contour[i:j])
        contour1.append(self.contour[j])

        if len(contour1) < 5:
            return [self]

        new_thickness = np.random.randint(1, self.thickness)

        return np.array(
            [
                FlakeObject(self.contour, self.thickness),
                FlakeObject(contour1, new_thickness),
            ]
        )


class ContourImage:
    def __init__(self, flakes) -> None:
        self.flakes: list[FlakeObject] = flakes

    def add_flake(self, flake: FlakeObject):
        self.flakes.append(flake)

    def draw_flakemask(self, resolution=(1200, 1920)):
        mask = np.zeros(resolution, dtype=np.uint16)
        for flake in self.flakes:
            placeholder = np.zeros(resolution, dtype=np.uint16)
            cv2.drawContours(placeholder, [flake.contour], -1, flake.thickness, -1)
            mask = mask + placeholder
        return mask
