import pytest
import numpy as np
from hardware import iocam_helpers as ih

def test_odd():
    assert ih._odd(5) == 5  # Already odd
    assert ih._odd(4) == 5  # Convert to odd
    assert ih._odd(0) == 1  # Handle zero
    assert ih._odd(-3) == -3  # Handle negative odd
    assert ih._odd(-4) == -3  # Handle negative even

def test_compute_fx_fy_from_fov():
    # Test with both FOVs provided
    fx, fy = ih.compute_fx_fy_from_fov(640, 480, 60.0, 45.0)
    assert fx is not None
    assert fy is not None
    
    # Test with only horizontal FOV
    fx, fy = ih.compute_fx_fy_from_fov(640, 480, 60.0, None)
    assert fx is not None
    assert fy is not None
    assert abs(fy - fx * (480/640)) < 1e-6
    
    # Test with only vertical FOV
    fx, fy = ih.compute_fx_fy_from_fov(640, 480, None, 45.0)
    assert fx is not None
    assert fy is not None
    assert abs(fx - fy * (640/480)) < 1e-6

def test_footprint_from_rect_pinhole():
    # Test with square aspect ratio
    w_mm, h_mm = ih.footprint_from_rect_pinhole(100, 100, 1000, 500, 500)
    assert w_mm == h_mm  # Should be square since input and focal lengths are equal
    assert abs(w_mm - 200) < 1e-6  # At 1000mm with f=500, 100px should be 200mm
    
    # Test with different aspect ratio
    w_mm, h_mm = ih.footprint_from_rect_pinhole(200, 100, 1000, 500, 500)
    assert abs(w_mm - 2*h_mm) < 1e-6  # Width should be twice height

def test_detect_checkerboard_scale_detailed():
    # Create a synthetic checkerboard image
    pattern_size = (7, 7)
    square_size = 30
    img_size = (480, 640)
    
    # Create blank image
    img = np.zeros(img_size, dtype=np.uint8)
    
    # Draw white squares in a checkerboard pattern
    square_h = img_size[0] // pattern_size[0]
    square_w = img_size[1] // pattern_size[1]
    
    for i in range(pattern_size[0]):
        for j in range(pattern_size[1]):
            if (i + j) % 2 == 0:
                y1, y2 = i*square_h, (i+1)*square_h
                x1, x2 = j*square_w, (j+1)*square_w
                img[y1:y2, x1:x2] = 255
    
    result = ih.detect_checkerboard_scale_detailed(img, square_size)
    
    assert result is not None
    assert 'pattern' in result
    assert 'corners' in result
    assert 'px_per_mm' in result
    assert result['px_per_mm'] > 0