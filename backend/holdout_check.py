"""
holdout_check.py  --  Does the layer-deviation curve beat a constant?

v2. The previous version compared predicted and observed MEANS, which is a weak
test: every plan averages about 1% deviation, so a predictor that ignores its
input entirely scores well on that comparison. It also applied an arbitrary
"within a factor of 2" verdict whose result flipped with the bin count.

This version asks the only question that matters for a per-layer flag:

    Does knowing a layer's meterset improve the prediction of its deviation,
    relative to simply predicting the training-set mean for every layer?

Reported per held-out plan:
    rank rho     -- does the curve ORDER that plan's layers correctly?
    MAE curve    -- mean absolute error of the binned prediction
    MAE const    -- mean absolute error of the training-mean constant
    skill        -- 1 - MAE_curve/MAE_const. Positive means the curve helps;
                    zero or negative means the meterset carries no usable
                    information for that plan.

Also prints each plan's layer-MU range, since a plan whose layers all sit in
one bin cannot be ordered by a bin-based predictor regardless of whether the
underlying mechanism is real.

Aborted/interrupted records report -100% deviation on every control point and
are filtered by default.

Usage (from virtual-psqa\\backend):
    ..\\python\\python.exe holdout_check.py --csv layers.csv
    ..\\python\\python.exe holdout_check.py --csv layers.csv --bins 3
    ..\\python\\python.exe holdout_check.py --csv layers.csv --continuous
"""

import argparse
import csv
import math
import sys

ABORT_DEV_PCT = 99.0


def load(path, exclude_fx, max_dev):
    rows, n_abort, n_fx = [], 0, 0
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                fid = int(r["fraction_id"])
                pid = int(r["plan_id"])
                mu = float(r["spec_mu"])
                dev = abs(float(r["abs_pct_dev"]))
            except (KeyError, ValueError):
                continue
            if fid in exclude_fx:
                n_fx += 1
                continue
            if dev >= max_dev:
                n_abort += 1
                continue
            if mu <= 0:
                continue
            rows.append((pid, fid, mu, dev))
    return rows, n_abort, n_fx


def fit_bins(rows, nbins):
    data = sorted(((mu, dev) for _, _, mu, dev in rows), key=lambda t: t[0])
    n = len(data)
    if n < nbins * 10:
        return None
    cuts = [round(i * n / nbins) for i in range(nbins)] + [n]
    out = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        seg = data[a:b]
        if not seg:
            continue
        devs = sorted(d for _, d in seg)
        mean = sum(devs) / len(devs)
        p95 = devs[min(len(devs) - 1, int(0.95 * len(devs)))]
        out.append((seg[0][0], mean, p95, len(seg)))
    out.sort(key=lambda t: -t[0])
    return out


def predict_bin(mu, bins):
    for lo, mean, p95, _ in bins:
        if mu >= lo:
            return mean
    return bins[-1][1]


def fit_loglinear(rows):
    """dev = a + b*log(mu), least squares. A smooth alternative to bins."""
    xs = [math.log(mu) for _, _, mu, _ in rows]
    ys = [dev for _, _, _, dev in rows]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def mae(pred, obs):
    return sum(abs(p - o) for p, o in zip(pred, obs)) / len(obs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="layers.csv")
    ap.add_argument("--bins", type=int, default=5)
    ap.add_argument("--continuous", action="store_true",
                    help="use a log-linear fit instead of quantile bins")
    ap.add_argument("--exclude-fractions", default="")
    ap.add_argument("--max-dev", type=float, default=ABORT_DEV_PCT)
    args = ap.parse_args()

    excl = {int(x) for x in args.exclude_fractions.split(",") if x.strip()}
    rows, n_abort, n_fx = load(args.csv, excl, args.max_dev)
    if not rows:
        sys.exit("no usable rows")

    plans = sorted({p for p, _, _, _ in rows})
    model = "log-linear" if args.continuous else f"{args.bins} quantile bins"
    print(f"{len(rows)} control points, {len(plans)} plans, model = {model}")
    if n_abort:
        print(f"dropped {n_abort} cp with |dev| >= {args.max_dev}% (aborted)")
    if n_fx:
        print(f"dropped {n_fx} cp from excluded fractions")

    # ---------- layer MU coverage per plan ----------
    print("\nLAYER MU RANGE (a plan spanning one bin cannot be ordered)")
    print(f"  {'plan':>6}{'n_cp':>7}{'min':>9}{'p25':>9}{'median':>9}"
          f"{'p75':>9}{'max':>10}")
    for p in plans:
        mus = sorted(mu for pid, _, mu, _ in rows if pid == p)
        n = len(mus)
        q = lambda f: mus[min(n - 1, int(f * n))]
        print(f"  {p:>6}{n:>7}{mus[0]:>9.2f}{q(.25):>9.2f}{q(.5):>9.2f}"
              f"{q(.75):>9.2f}{mus[-1]:>10.2f}")

    # ---------- leave one plan out ----------
    print("\nLEAVE-ONE-PLAN-OUT vs CONSTANT BASELINE")
    print(f"  {'held out':>9}{'n_cp':>7}{'rank rho':>10}{'MAE curve':>11}"
          f"{'MAE const':>11}{'skill':>8}")
    skills = []
    for p in plans:
        train = [r for r in rows if r[0] != p]
        test = [r for r in rows if r[0] == p]
        if len(test) < 10:
            print(f"  {p:>9}{len(test):>7}   insufficient data")
            continue

        obs = [dev for _, _, _, dev in test]
        const = sum(d for _, _, _, d in train) / len(train)

        if args.continuous:
            fit = fit_loglinear(train)
            if fit is None:
                continue
            a, b = fit
            preds = [a + b * math.log(mu) for _, _, mu, _ in test]
        else:
            bins = fit_bins(train, args.bins)
            if not bins:
                continue
            preds = [predict_bin(mu, bins) for _, _, mu, _ in test]

        rho = spearman(preds, obs)
        m_curve = mae(preds, obs)
        m_const = mae([const] * len(obs), obs)
        skill = 1.0 - (m_curve / m_const) if m_const else float("nan")
        skills.append(skill)
        rs = f"{rho:>10.3f}" if rho is not None else f"{'n/a':>10}"
        print(f"  {p:>9}{len(test):>7}{rs}{m_curve:>11.3f}"
              f"{m_const:>11.3f}{skill:>8.3f}")

    if skills:
        print(f"\n  skill range: {min(skills):.3f} to {max(skills):.3f}")
        print("  skill > 0 means layer meterset improved on a constant.")
        if min(skills) <= 0:
            n_bad = sum(1 for s in skills if s <= 0)
            print(f"\n  >>> {n_bad} of {len(skills)} held-out plans show NO skill.")
            print("  >>> On those plans, knowing the layer meterset did not")
            print("  >>> improve the prediction. A per-layer flag is not")
            print("  >>> supportable on plans outside the fitted set.")
        else:
            print("\n  >>> Positive skill on every held-out plan.")

    # ---------- within-plan mechanism check ----------
    print("\nWITHIN-PLAN rank correlation (MU vs |dev|), fitted to nothing")
    print(f"  {'plan':>6}{'n_cp':>7}{'rho':>9}")
    for p in plans:
        sel = [(mu, dev) for pid, _, mu, dev in rows if pid == p]
        rho = spearman([m for m, _ in sel], [d for _, d in sel])
        rs = f"{rho:>9.3f}" if rho is not None else f"{'n/a':>9}"
        print(f"  {p:>6}{len(sel):>7}{rs}")
    print("\n  This is the mechanism itself, plan by plan, with no model in")
    print("  the way. If it is strong everywhere, the effect is real and the")
    print("  modelling is at fault. If it varies, the effect is not universal.")


if __name__ == "__main__":
    main()
