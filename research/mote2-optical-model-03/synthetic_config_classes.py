from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratorMaterial:
    name: str
    thickness_per_layer: float
    substrate_thickness_range: tuple[int, int]

    def to_dict(self):
        return self.__dict__


@dataclass(frozen=True)
class GeneratorConfig:
    material: GeneratorMaterial
    white_balance_r_range: tuple[float, float] = (1, 1)
    white_balance_g_range: tuple[float, float] = (1, 1)
    white_balance_b_range: tuple[float, float] = (1, 1)
    num_flakes_range: tuple[int, int] = (1, 200)
    brightness_range: tuple[int, int] = (20, 20)
    shadow_probability: float = 0
    tape_probability: float = 0
    color_clip_value: int = 255
    size_threshold: int = 300
    max_thickness: int = 100
    num_images: int = 10000

    def to_dict(self):
        temp_generator_config = self.__dict__.copy()
        temp_generator_config["material"] = self.material.to_dict()
        return temp_generator_config
