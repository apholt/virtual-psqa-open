# Citations & Scientific Acknowledgments

Virtual PSQA is built upon foundational open-source medical imaging software, scientific computing libraries, and published radiation oncology quality assurance methodologies. 

If you use Virtual PSQA in your research, clinical studies, or institutional presentations, please cite the following open-source projects, scientific publications, and AAPM Task Group reports.

---

## 1. Project Authorship & Primary Development

* **Lead Developer & System Architect:** **Aaron Hutchins** ([ahutchins180@gmail.com](mailto:ahutchins180@gmail.com))  
  *Principal author and architect who conceived, designed, and developed the core Virtual PSQA codebase, DICOM ingestion engine, openMCsquare simulation pipelines, fractional delivery log reconstruction, and clinical QA decision engine.*
* **Contributor:** **Adam Holt** ([sebaldus.adam@gmail.com](mailto:sebaldus.adam@gmail.com) / GitHub: [`@apholt`](https://github.com/apholt))  
  *Codebase polish, quality-of-life enhancements, openMCsquare scenario robustness analysis, DVH prediction module, and open-source release preparation.*

### Suggested Platform Citation
If referencing or citing the Virtual PSQA platform in publications or presentations:
> Hutchins, A. & Holt, A. *Virtual PSQA: Independent Monte Carlo Secondary Dose Calculation and Fractional Delivery Verification Platform for Proton Pencil Beam Scanning Radiotherapy.* 2026. [https://github.com/apholt/virtual-psqa-open](https://github.com/apholt/virtual-psqa-open)

---

## 2. Core Medical Physics & Imaging Software

### openMCsquare
*Fast Monte Carlo dose calculation engine for proton therapy.*
* **Authors:** Kevin Souris, John A. Lee, et al. (Université catholique de Louvain)
* **Citation:** Souris K, Lee JA, Sterpin E. *Fast Monte Carlo dose calculation for proton beam therapy using openMCsquare.* **Medical Physics**, 2016; 43(8):4857–4868. doi:[10.1118/1.4959112](https://doi.org/10.1118/1.4959112).
* **Repository:** [https://github.com/openMCsquare/MCsquare](https://github.com/openMCsquare/MCsquare)

### Orthanc
*Lightweight, open-source DICOM server and DICOMWeb VNA platform.*
* **Author:** Sébastien Jodogne, et al. (UCLouvain / Osimis)
* **Citation:** Jodogne S. *The Orthanc Ecosystem for Medical Imaging.* **Journal of Digital Imaging**, 2018; 31(3):341–352. doi:[10.1007/s10278-018-0082-y](https://doi.org/10.1007/s10278-018-0082-y).
* **Website:** [https://www.orthanc-server.com](https://www.orthanc-server.com)

### SimpleITK / Insight Segmentation and Registration Toolkit (ITK)
*Deformable Image Registration (DIR) and rigid transformation framework used in Virtual PSQA's Synthetic CT engine.*
* **Authors:** Bradley C. Lowekamp, David T. Chen, Luis Ibáñez, Daniel Blezek, et al.
* **Citation:** Lowekamp BC, Chen DT, Ibáñez L, Blezek D. *The Design of SimpleITK.* **Frontiers in Neuroinformatics**, 2013; 7:45. doi:[10.3389/fninf.2013.00045](https://doi.org/10.3389/fninf.2013.00045).
* **Website:** [https://simpleitk.org](https://simpleitk.org)

### pydicom
*Python package for parsing, inspecting, and writing standard DICOM files (RT Ion Plan, RT Ion Record, RT Dose, RTSTRUCT, CT/CBCT).*
* **Authors:** Darcy Mason, et al.
* **Citation:** Mason D, et al. *pydicom: An open source DICOM library.* Zenodo. doi:[10.5281/zenodo.592552](https://doi.org/10.5281/zenodo.592552).
* **Repository:** [https://github.com/pydicom/pydicom](https://github.com/pydicom/pydicom)

### WeasyPrint
*Visual rendering engine converting HTML5/CSS3 templates into print-ready PDF reports for electronic Oncology Management Record (OMR) upload.*
* **Authors:** Kozea Community / Simon Sapin, et al.
* **Website:** [https://weasyprint.org](https://weasyprint.org)

---

## 3. Scientific Algorithms & Methods

### Fast 2D & 3D Gamma Evaluation
*Vectorized local-search algorithm implemented in Virtual PSQA's `gamma_engine`.*
* **Authors:** M. Wendling, L. J. Zijp, G. M. Janssen, et al.
* **Citation:** Wendling M, Zijp LJ, Janssen GM, et al. *A fast algorithm for gamma evaluation in 2D and 3D.* **Medical Physics**, 2007; 34(5):1647–1654. doi:[10.1118/1.2721657](https://doi.org/10.1118/1.2721657).

### Foundational Gamma Index Definition
*The standard mathematical definition of the gamma evaluation index.*
* **Authors:** Daniel A. Low, William B. Harms, Sasa Mutic, James A. Purdy
* **Citation:** Low DA, Harms WB, Mutic S, Purdy JA. *A technique for the quantitative evaluation of dose distributions.* **Medical Physics**, 1998; 25(5):656–661. doi:[10.1118/1.598248](https://doi.org/10.1118/1.598248).

### Diffeomorphic Demons Deformable Registration
*Non-parametric deformable registration algorithm used to map planning CT onto daily CBCT for Synthetic CT calculation.*
* **Authors:** Tom Vercauteren, Xavier Pennec, Aymeric Perchant, Nicholas Ayache
* **Citation:** Vercauteren T, Pennec X, Perchant A, Ayache N. *Diffeomorphic demons: Efficient non-parametric image registration.* **NeuroImage**, 2009; 45(1):S61–S72. doi:[10.1016/j.neuroimage.2008.10.040](https://doi.org/10.1016/j.neuroimage.2008.10.040).

### Machine Delivery Perturbation & Systematics
*Physics-based room systematic deviation modeling for pencil beam scanning.*
* **Authors:** S. Toscano, et al.
* **Citation:** Toscano S, et al. *Machine delivery perturbation modeling in proton therapy.* **Physics in Medicine & Biology**, 2019; 64(9):095021. doi:[10.1088/1361-6560/ab15b4](https://doi.org/10.1088/1361-6560/ab15b4).

### Modulation Complexity Metric for PBS
*Quantitative plan modulation metric for proton pencil beam scanning spot arrays.*
* **Citation:** McNiven AL, Sharpe MB, Purdie TG. *A new metric for assessing IMRT modulation: the modulation complexity score.* **Medical Physics**, 2010; 37(2):517–525. doi:[10.1118/1.3276775](https://doi.org/10.1118/1.3276775).

---

## 4. AAPM Clinical Quality Assurance Consensus Reports

* **AAPM Task Group 218:**  
  Miften M, Olch A, Mihailidis D, et al. *Tolerance limits and methodologies for IMRT measurement-based verification QA: Recommendations of AAPM Task Group No. 218.* **Medical Physics**, 2018; 45(4):e53–e83. doi:[10.1002/mp.12810](https://doi.org/10.1002/mp.12810).
* **AAPM Task Group 275:**  
  Ford E, Conroy L, Dong L, et al. *Strategies for effective physics plan and chart review in radiation therapy: Report of AAPM Task Group 275.* **Medical Physics**, 2020; 47(6):e236–e273. doi:[10.1002/mp.14120](https://doi.org/10.1002/mp.14120).
* **AAPM Task Group 142:**  
  Klein EE, Hanley J, Bayouth J, et al. *Task Group 142 report: Quality assurance of medical accelerators.* **Medical Physics**, 2009; 36(9):4197–4212. doi:[10.1118/1.3190392](https://doi.org/10.1118/1.3190392).
* **AAPM Task Group 224:**  
  Arjomandy B, Sahoo N, Zhu XR, et al. *AAPM medical physics practice guideline 5.a.: Commissioning and QA of treatment planning dose calculation algorithms for proton therapy.* **Journal of Applied Clinical Medical Physics**, 2019; 20(8):7–30. doi:[10.1002/acm2.12658](https://doi.org/10.1002/acm2.12658).

---

## 5. Scientific Python Stack

* **NumPy:** Harris CR, Millman KJ, van der Walt SJ, et al. *Array programming with NumPy.* **Nature**, 2020; 585:357–362. doi:[10.1038/s41586-020-2649-2](https://doi.org/10.1038/s41586-020-2649-2).
* **SciPy:** Virtanen P, Gommers R, Oliphant TE, et al. *SciPy 1.0: Fundamental Algorithms for Scientific Computing in Python.* **Nature Methods**, 2020; 17:261–272. doi:[10.1038/s41592-019-0686-2](https://doi.org/10.1038/s41592-019-0686-2).
* **Scikit-learn:** Pedregosa F, Varoquaux G, Gramfort A, et al. *Scikit-learn: Machine Learning in Python.* **Journal of Machine Learning Research**, 2011; 12:2825–2830.
* **Matplotlib:** Hunter JD. *Matplotlib: A 2D Graphics Environment.* **Computing in Science & Engineering**, 2007; 9(3):90–95. doi:[10.1109/MCSE.2007.55](https://doi.org/10.1109/MCSE.2007.55).

---

## 6. BibTeX Entries

```bibtex
@misc{hutchins2026virtualpsqa,
  title={{Virtual PSQA}: Independent {Monte Carlo} Secondary Dose Calculation and Fractional Delivery Verification Platform for Proton Pencil Beam Scanning Radiotherapy},
  author={Hutchins, Aaron and Holt, Adam},
  year={2026},
  howpublished={\url{https://github.com/apholt/virtual-psqa-open}}
}

@article{souris2016fast,
  title={Fast {Monte Carlo} dose calculation for proton beam therapy using open{MCsquare}},
  author={Souris, K{\'e}vin and Lee, John A and Sterpin, Edmond},
  journal={Medical Physics},
  volume={43},
  number={8},
  pages={4857--4868},
  year={2016},
  doi={10.1118/1.4959112}
}

@article{jodogne2018orthanc,
  title={The {Orthanc} Ecosystem for Medical Imaging},
  author={Jodogne, S{\'e}bastien},
  journal={Journal of Digital Imaging},
  volume={31},
  number={3},
  pages={341--352},
  year={2018},
  doi={10.1007/s10278-018-0082-y}
}

@article{lowekamp2013design,
  title={The Design of {SimpleITK}},
  author={Lowekamp, Bradley C and Chen, David T and Ib{\'a}{\~n}ez, Luis and Blezek, Daniel},
  journal={Frontiers in Neuroinformatics},
  volume={7},
  pages={45},
  year={2013},
  doi={10.3389/fninf.2013.00045}
}

@article{wendling2007fast,
  title={A fast algorithm for gamma evaluation in {2D} and {3D}},
  author={Wendling, Markus and Zijp, Lourens J and Janssen, Geert M and others},
  journal={Medical Physics},
  volume={34},
  number={5},
  pages={1647--1654},
  year={2007},
  doi={10.1118/1.2721657}
}

@article{low1998technique,
  title={A technique for the quantitative evaluation of dose distributions},
  author={Low, Daniel A and Harms, William B and Mutic, Sasa and Purdy, James A},
  journal={Medical Physics},
  volume={25},
  number={5},
  pages={656--661},
  year={1998},
  doi={10.1118/1.598248}
}

@article{vercauteren2009diffeomorphic,
  title={Diffeomorphic demons: Efficient non-parametric image registration},
  author={Vercauteren, Tom and Pennec, Xavier and Perchant, Aymeric and Ayache, Nicholas},
  journal={NeuroImage},
  volume={45},
  number={1},
  pages={S61--S72},
  year={2009},
  doi={10.1016/j.neuroimage.2008.10.040}
}

@article{miften2018tolerance,
  title={Tolerance limits and methodologies for {IMRT} measurement-based verification {QA}: Recommendations of {AAPM} Task Group No. 218},
  author={Miften, Moyed and Olch, Arthur and Mihailidis, Dimitris and others},
  journal={Medical Physics},
  volume={45},
  number={4},
  pages={e53--e83},
  year={2018},
  doi={10.1002/mp.12810}
}

@article{ford2020strategies,
  title={Strategies for effective physics plan and chart review in radiation therapy: Report of {AAPM} Task Group 275},
  author={Ford, Eric and Conroy, Leigh and Dong, Lei and others},
  journal={Medical Physics},
  volume={47},
  number={6},
  pages={e236--e273},
  year={2020},
  doi={10.1002/mp.14120}
}
```
