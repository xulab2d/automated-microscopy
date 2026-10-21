import numpy as np
import torch

from .draw import (
    get_semantic_mask,
    add_blur,
    add_camera_noise,
    add_shadow,
    add_tape_residue,
    color_image,
    get_random_mask,
)


def generate_image_and_mask(
    material_name: str,
    material_thickness_per_layer: float,
    refractive_indices: dict,
    wavelengths: torch.Tensor | np.ndarray,
    camera_activation: np.ndarray,
    spectrum: np.ndarray,
    contours: list[np.ndarray],
    use_tape: bool = False,
    use_shadow: bool = False,
    size_threshold: int = 300,
    max_thickness: int = 100,
    color_clip_value: float = 255,
    num_flakes: int = 20,
    white_balance: tuple[float, float, float] = (1.0, 1.0, 1.0),
    brightness: float = 20.0,
    substrate_thickness: float = 90,
    device: torch.device = torch.device("cuda" if torch.cuda.is_available() else "cpu"),
) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate a synthetic image and corresponding mask for material analysis.
    This function creates a realistic image of material flakes with optical properties
    and a corresponding semantic mask. The image generation includes physical modeling
    of light interaction, optional tape residue, shadows, camera noise, and blur effects.
    Args:
        material_name (str): Name of the material to simulate.
        material_thickness_per_layer (float): Thickness of each material layer.
        refractive_indices (dict): Dictionary mapping wavelengths to refractive index values.
        wavelengths (torch.Tensor | np.ndarray): Array of wavelength values for optical simulation.
        camera_activation (np.ndarray): Camera sensor activation response curve.
        spectrum (np.ndarray): Light spectrum data for illumination.
        contours (list[np.ndarray]): List of contour arrays defining possible flake shapes.
        use_tape (bool, optional): Whether to add tape residue effects. Defaults to False.
        use_shadow (bool, optional): Whether to add shadow effects. Defaults to False.
        size_threshold (int, optional): Minimum size threshold for mask filtering. Defaults to 300.
        max_thickness (int, optional): Maximum thickness for semantic mask generation. Defaults to 100.
        color_clip_value (float, optional): Maximum color value for clipping. Defaults to 255.
        num_flakes (int, optional): Number of material flakes to generate. Defaults to 20.
        white_balance (tuple[float, float, float], optional): RGB white balance factors. Defaults to (1.0, 1.0, 1.0).
        brightness (float, optional): Overall brightness adjustment factor. Defaults to 20.0.
        substrate_thickness (float, optional): Thickness of the substrate layer in nm. Defaults to 90.
        device (torch.device, optional): PyTorch device for computation. Defaults to CUDA if available, else CPU.
    Returns:
        tuple[np.ndarray, np.ndarray]: A tuple containing:
            - image (np.ndarray): Generated synthetic image with realistic material appearance
            - mask (np.ndarray): Corresponding semantic mask with size and thickness filtering applied
    Note:
        The function performs several image processing steps in sequence:
        1. Generates random flake positions using provided contours
        2. Applies optical color modeling based on material properties
        3. Optionally adds tape residue effects
        4. Adds realistic camera noise
        5. Optionally adds shadow effects
        6. Applies blur for realism
        7. Creates semantic mask with size and thickness constraints
    """

    original_mask = get_random_mask(num_flakes, contours)
    mask = original_mask.copy()

    if type(wavelengths) is np.ndarray:
        wavelengths = torch.tensor(wavelengths, dtype=torch.float32, device=device)

    image = color_image(
        mask,
        material=material_name,
        thickness_per_layer=material_thickness_per_layer,
        white_balance=white_balance,
        brightness=brightness,
        substrate_thickness=substrate_thickness,
        refractive_indices=refractive_indices,
        wavelengths=wavelengths,
        camera_activation=camera_activation,
        spectrum=spectrum,
        device=device,
    )

    if use_tape:
        image = add_tape_residue(
            image=image,
            mask=mask,
            tape_mask=get_random_mask(num_flakes // 10, contours),
            white_balance=white_balance,
            brightness=brightness,
            substrate_thickness=substrate_thickness,
            wavelengths=wavelengths,
            camera_activation=camera_activation,
            refractive_indices=refractive_indices,
            spectrum=spectrum,
            device=device,
        )

    image = add_camera_noise(
        image,
        brightness,
        color_clip_value,
    )

    if use_shadow:
        image, original_mask = add_shadow(image, original_mask)

    image = add_blur(image)

    # 200 in pixel units is about ~25 microns
    mask = get_semantic_mask(
        original_mask,
        size_threshold,
        max_thickness,
    )

    return image, mask
