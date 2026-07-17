# TopoR reference images & documents

Local copies of **public marketing and support assets** from [Eremex TopoR](https://www.eremex.com/products/topor/), used in [docs/TOPOR.md](../../TOPOR.md).

## License / attribution

© Eremex, Ltd. Images and PDFs remain the property of Eremex. They are cached here for offline research and for documenting how **physicsRouter** relates to commercial TopoR concepts. Do not redistribute as your own product materials. Prefer linking to [eremex.com](https://www.eremex.com/) for official downloads.

| Source | URL |
|--------|-----|
| Product | https://www.eremex.com/products/topor/ |
| Features | https://www.eremex.com/products/topor/features/ |
| Autorouting advantages | https://www.eremex.com/products/topor/competitiveadvantages/autorouting/ |
| Design time | https://www.eremex.com/products/topor/competitiveadvantages/pcbdesigntime/ |
| High-speed | https://www.eremex.com/products/topor/competitiveadvantages/highspeedpcbs/ |
| Downloads (login) | https://www.eremex.com/downloads/ |
| Version history | https://www.eremex.com/support/topor-version-history/ |
| Publications | https://www.eremex.com/support/publications/ |

## Files

| File | Topic |
|------|--------|
| `Topor70.gif` | Product banner |
| `topor_main_demo.gif` | Product page demo topology |
| `topor_via_reduce.jpg` | Via / cost reduction messaging |
| `topor_emc.jpg` | Crosstalk / free-angle EMC claim |
| `topor_single_layer.jpg` | Single-layer routing claim |
| `isotropic_fragment.jpg` | Free-angle + arcs topology fragment |
| `arcs_space.jpg` | Arc routing packing board surface |
| `equal_spacing.jpg` | Equal conductor spacing vs 45° |
| `single_layer_topor.jpg` / `single_layer_shape_based.jpg` | Single-layer vs shape-based |
| `flex_topor.jpg` / `flex_shape_based.jpg` | Flex PCB via/length comparison |
| `multilayer_2L_topor.jpg` / `multilayer_8L_shape.jpg` | 2L TopoR vs 8L conventional |
| `bga_topor.jpg` / `bga_shape_based.jpg` | BGA area routing comparison |
| `pin_equiv_on.jpg` / `pin_equiv_off.jpg` | Logical pin equivalence |
| `feature_*.{jpg,gif}` | Feature list thumbnails |
| `topology_touch.jpg` / `topology_strung.jpg` | Topology → geometry “stringing” |
| `move_cap_*.jpg`, `auto_move.jpg` | Component move with live re-geometry |
| `manual_2wk.jpg` / `topor_1hr.jpg` | Productivity case study |
| `length_limit.jpg` / `trapezoid_tune.jpg` | High-speed length / trapezoid tuning |
| `TopoR_datasheet.pdf` | Official feature datasheet (public) |
| `TopoR_6.1_user_manual.pdf` | English user manual (public, Jul 2015) |

## Re-download

```bash
# From repo root — sized Bitrix thumbnails need curl --globoff
curl --globoff -fsSL -A "Mozilla/5.0" \
  -o docs/images/topor/topor_main_demo.gif \
  'https://www.eremex.com/images/383068$[352x259].gif'
# Full-res examples often work without size suffix, e.g.:
curl -fsSL -o docs/images/topor/isotropic_fragment.jpg \
  https://www.eremex.com/images/434662.jpg
```

Installer packages (Lite/Trial 7.0.18508) require an Eremex account; unauthenticated download endpoints currently 404. Contact `info@eremex.com` if login download fails.
