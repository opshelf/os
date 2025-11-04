# #!/usr/bin/env python3
# """
# Depth camera calibration (linear & nonlinear) with safe bounds and data-aware initial guesses.

# Examples:
#   # Fit from inline samples (Z_true:mm : D_raw), auto-pick best model, show plots
#   python depth_calibrate.py --samples 617:517 499:507 485:447 --auto --show

#   # Force linear with the same samples
#   python depth_calibrate.py --samples 617:517 499:507 485:447 --model linear

#   # Fit from CSV with columns Z_true,D_raw and save params
#   python depth_calibrate.py --csv calib_points.csv --auto --save calib.json

#   # Predict using a saved calibration
#   python depth_calibrate.py --load calib.json --predict 450 500 650

# CSV format:
#   Z_true,D_raw
#   300,4012
#   400,5310
#   ...
# """

# import argparse, json, math, sys
# from dataclasses import dataclass
# from typing import Callable, Dict, List, Tuple, Optional

# import numpy as np
# from numpy.typing import ArrayLike

# try:
#     from scipy.optimize import curve_fit
# except Exception as e:
#     print("ERROR: This script requires SciPy. Install with: pip install scipy", file=sys.stderr)
#     raise

# # ---------- Optional plotting ----------
# def maybe_import_matplotlib():
#     try:
#         import matplotlib.pyplot as plt
#         return plt
#     except Exception:
#         return None

# # ---------- Models ----------
# def m_linear(D, a, b):
#     return a*D + b

# def m_quadratic(D, a, b, c):
#     return a*D + b + c*(D**2)

# def m_affine_recip(D, a, b, c, d):
#     # Z = a*D + b + c/(D + d)
#     return a*D + b + c / (D + d)

# def m_rational_1over1(D, a, b, c, d):
#     # Z = (a*D + b) / (1 + c*D) + d
#     return (a*D + b) / (1.0 + c*D) + d

# # p0 makers
# def make_p0_linear(D, Z):
#     dspan = max(float(D.max() - D.min()), 1.0)
#     a0 = (float(Z[-1] - Z[0])) / dspan
#     b0 = float(Z.mean() - a0 * D.mean())
#     return (a0, b0)

# def make_p0_quadratic(D, Z):
#     a0, b0 = make_p0_linear(D, Z)
#     return (a0, b0, 0.0)

# def make_p0_affine_recip(D, Z):
#     a0, b0 = make_p0_linear(D, Z)
#     d0 = -0.5 * (float(D.min()) + float(D.max()))  # pole to the left of data
#     c0 = 0.0
#     return (a0, b0, c0, d0)

# def make_p0_rational_1over1(D, Z):
#     a0, b0 = make_p0_linear(D, Z)
#     c0 = 0.0
#     d0 = 0.0
#     return (a0, b0, c0, d0)

# MODELS: Dict[str, Dict[str, object]] = {
#     "linear": {
#         "fun": m_linear,
#         "p0": make_p0_linear,
#         "n": 2
#     },
#     "quadratic": {
#         "fun": m_quadratic,
#         "p0": make_p0_quadratic,
#         "n": 3
#     },
#     "affine_recip": {
#         "fun": m_affine_recip,
#         "p0": make_p0_affine_recip,
#         "n": 4
#     },
#     "rational_1over1": {
#         "fun": m_rational_1over1,
#         "p0": make_p0_rational_1over1,
#         "n": 4
#     },
# }

# def bounds_for(name: str, D: np.ndarray):
#     Dmin, Dmax = float(D.min()), float(D.max())
#     if name == "linear":
#         return ([-np.inf, -np.inf], [np.inf, np.inf])
#     if name == "quadratic":
#         # keep curvature modest; relax if you know your sensor is more nonlinear
#         return ([-np.inf, -np.inf, -1e-4], [np.inf, np.inf, 1e-4])
#     if name == "affine_recip":
#         # avoid (D + d) crossing zero in the observed range
#         lb_d = -Dmax + 1.0
#         ub_d = -Dmin - 1.0
#         return ([-np.inf, -np.inf, -1e6, lb_d], [np.inf, np.inf, 1e6, ub_d])
#     if name == "rational_1over1":
#         # keep denominator positive in range
#         c_low = -1.0 / (Dmax + 1.0)
#         c_high =  1.0 / max(Dmin, 1.0)
#         return ([-np.inf, -np.inf, c_low, -1e6], [np.inf, np.inf, c_high, 1e6])
#     return (-np.inf, np.inf)

# def clamp_p0_to_bounds(p0, lb, ub):
#     p0 = np.asarray(p0, float)
#     lb = np.asarray(lb, float)
#     ub = np.asarray(ub, float)
#     eps = 1e-9
#     p0 = np.minimum(np.maximum(p0, lb + eps), ub - eps)
#     return tuple(p0.tolist())

# # ---------- Metrics / selection ----------
# def metrics(z_true: ArrayLike, z_pred: ArrayLike) -> Dict[str, float]:
#     z_true = np.asarray(z_true)
#     z_pred = np.asarray(z_pred)
#     resid = z_true - z_pred
#     rmse = float(np.sqrt(np.mean(resid**2)))
#     mae  = float(np.mean(np.abs(resid)))
#     ss_res = float(np.sum(resid**2))
#     ss_tot = float(np.sum((z_true - np.mean(z_true))**2))
#     r2 = 1.0 - ss_res/ss_tot if ss_tot > 0 else float("nan")
#     return {"rmse": rmse, "mae": mae, "r2": r2, "ss_res": ss_res}

# def aic(n: int, ss_res: float, k: int) -> float:
#     if n <= k + 1 or ss_res <= 0:
#         return float("inf")
#     sigma2 = ss_res / n
#     return float(n * (1.0 + math.log(2*math.pi*sigma2)) + 2*(k+1))

# @dataclass
# class FitResult:
#     name: str
#     params: np.ndarray
#     z_pred: np.ndarray
#     stats: Dict[str, float]
#     aic: float

# def fit_model(name: str, D: np.ndarray, Z: np.ndarray) -> FitResult:
#     spec = MODELS[name]
#     fun  = spec["fun"]
#     p0   = spec["p0"](D, Z)
#     lb, ub = bounds_for(name, D)
#     p0 = clamp_p0_to_bounds(p0, lb, ub)
#     params, _cov = curve_fit(fun, D, Z, p0=p0, bounds=(lb, ub), maxfev=20000)
#     z_pred = fun(D, *params)
#     st = metrics(Z, z_pred)
#     ss_res = max(st["ss_res"], 1e-12)  # keep AIC finite
#     return FitResult(name=name, params=params, z_pred=z_pred, stats=st, aic=aic(len(D), ss_res, len(params)))

# def best_model(D: np.ndarray, Z: np.ndarray, candidates: List[str]) -> FitResult:
#     n = len(D)
#     usable = [m for m in candidates if n >= MODELS[m]["n"]]
#     fits: List[FitResult] = []
#     for m in usable:
#         try:
#             fits.append(fit_model(m, D, Z))
#         except Exception:
#             # Skip models that fail to fit
#             continue
#     if not fits:
#         raise RuntimeError("No models could be fit; add more samples or relax constraints.")
#     fits.sort(key=lambda fr: fr.aic)
#     return fits[0]

# # ---------- I/O ----------
# def parse_samples(pairs: List[str]) -> Tuple[np.ndarray, np.ndarray]:
#     z_true, d_raw = [], []
#     for p in pairs:
#         z_str, d_str = p.split(":")
#         z_true.append(float(z_str))
#         d_raw.append(float(d_str))
#     Z = np.array(z_true, dtype=float)
#     D = np.array(d_raw, dtype=float)
#     order = np.argsort(D)
#     return Z[order], D[order]

# def read_csv(path: str) -> Tuple[np.ndarray, np.ndarray]:
#     rows = np.genfromtxt(path, delimiter=",", names=True, dtype=None, encoding=None)
#     if "Z_true" not in rows.dtype.names or "D_raw" not in rows.dtype.names:
#         raise ValueError("CSV must have headers: Z_true,D_raw")
#     Z = np.asarray(rows["Z_true"], float)
#     D = np.asarray(rows["D_raw"], float)
#     order = np.argsort(D)
#     return Z[order], D[order]

# def save_json(path: str, name: str, params: np.ndarray):
#     data = {"model": name, "params": list(map(float, params))}
#     with open(path, "w") as f:
#         json.dump(data, f, indent=2)
#     print(f"Saved calibration → {path}")

# def load_json(path: str) -> Tuple[str, np.ndarray]:
#     with open(path, "r") as f:
#         data = json.load(f)
#     name = data["model"]
#     params = np.array(data["params"], float)
#     if name not in MODELS:
#         raise ValueError(f"Unknown model in JSON: {name}")
#     if len(params) != MODELS[name]["n"]:
#         raise ValueError(f"Param length mismatch for model '{name}'")
#     return name, params

# def pretty_params(name: str, params: np.ndarray) -> str:
#     if name == "linear":
#         a,b = params
#         return f"Z = {a:.8f} * D + {b:.6f}"
#     if name == "quadratic":
#         a,b,c = params
#         return f"Z = {a:.8f} * D + {b:.6f} + {c:.10f} * D^2"
#     if name == "affine_recip":
#         a,b,c,d = params
#         return f"Z = {a:.8f} * D + {b:.6f} + {c:.8f} / (D + {d:.6f})"
#     if name == "rational_1over1":
#         a,b,c,d = params
#         return f"Z = ({a:.8f} * D + {b:.6f}) / (1 + {c:.10f} * D) + {d:.6f}"
#     return f"{name}: {params}"

# # ---------- Main ----------
# def main():
#     ap = argparse.ArgumentParser()
#     src = ap.add_mutually_exclusive_group(required=False)
#     src.add_argument("--samples", nargs="+", help="Pairs 'Zmm:Draw' e.g. 300:4012 400:5310 ...")
#     src.add_argument("--csv", help="CSV with columns Z_true,D_raw")

#     ap.add_argument("--model", choices=list(MODELS.keys()), help="Pick a specific model")
#     ap.add_argument("--auto", action="store_true", help="Try multiple models and pick best by AIC")
#     ap.add_argument("--save", help="Save calibration JSON")
#     ap.add_argument("--load", help="Load calibration JSON (skips fitting unless combined with new data)")
#     ap.add_argument("--predict", nargs="+", type=float, help="Predict Z for provided raw D values")
#     ap.add_argument("--show", action="store_true", help="Plot fit & residuals (requires matplotlib)")

#     args = ap.parse_args()

#     Z, D = None, None
#     # Load data if provided
#     if args.samples:
#         Z, D = parse_samples(args.samples)
#     elif args.csv:
#         Z, D = read_csv(args.csv)

#     # If we only want to predict with a saved model:
#     if args.load and Z is None and D is None:
#         name, params = load_json(args.load)
#         if args.predict:
#             fun = MODELS[name]["fun"]
#             Dp = np.asarray(args.predict, float)
#             Zp = fun(Dp, *params)
#             print(f"\nLoaded model: {name}\n  {pretty_params(name, params)}\n")
#             for d, z in zip(Dp, Zp):
#                 print(f"D={d:.6f} → Z={z:.3f} mm")
#         else:
#             print(f"\nLoaded model: {name}\n  {pretty_params(name, params)}")
#         return

#     # Otherwise, we need to fit
#     if Z is None or D is None:
#         ap.error("Provide --samples or --csv, or use --load with --predict.")

#     # Fit path
#     if args.auto:
#         candidates = ["linear", "quadratic", "affine_recip", "rational_1over1"]
#         res = best_model(D, Z, candidates)
#     else:
#         name = args.model or "quadratic"  # mild nonlinearity default
#         if len(D) < MODELS[name]["n"]:
#             raise SystemExit(f"Need at least {MODELS[name]['n']} points for model '{name}', got {len(D)}.")
#         res = fit_model(name, D, Z)

#     print("\nDepth calibration result")
#     print(f"  Model:   {res.name}")
#     print(f"  Formula: {pretty_params(res.name, res.params)}")
#     print(f"  RMSE:    {res.stats['rmse']:.3f} mm")
#     print(f"  MAE:     {res.stats['mae']:.3f} mm")
#     print(f"  R²:      {res.stats['r2']:.6f}")
#     print(f"  AIC:     {res.aic:.2f}")

#     if args.save:
#         save_json(args.save, res.name, res.params)

#     # Prediction on demand
#     if args.predict:
#         fun = MODELS[res.name]["fun"]
#         Dp = np.asarray(args.predict, float)
#         Zp = fun(Dp, *res.params)
#         print("\nPredictions:")
#         for d, z in zip(Dp, Zp):
#             print(f"D={d:.6f} → Z={z:.3f} mm")

#     # Plot
#     if args.show:
#         plt = maybe_import_matplotlib()
#         if plt is None:
#             print("matplotlib not installed; skipping plots.", file=sys.stderr)
#         else:
#             fun = MODELS[res.name]["fun"]
#             Dline = np.linspace(D.min(), D.max(), 400)
#             Zline = fun(Dline, *res.params)

#             plt.figure(figsize=(6,4))
#             plt.scatter(D, Z, label="Measured")
#             plt.plot(Dline, Zline, label=f"Fit: {res.name}")
#             plt.xlabel("Raw depth (D)")
#             plt.ylabel("True distance Z (mm)")
#             plt.title("Depth Calibration Fit")
#             plt.legend()
#             plt.tight_layout()

#             plt.figure(figsize=(6,3))
#             resid = Z - res.z_pred
#             plt.axhline(0, linestyle="--", linewidth=1)
#             plt.scatter(D, resid)
#             plt.xlabel("Raw depth (D)")
#             plt.ylabel("Residual (mm)")
#             plt.title("Residuals")
#             plt.tight_layout()
#             plt.show()

# if __name__ == "__main__":
#     main()
