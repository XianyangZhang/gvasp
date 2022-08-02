import abc
import copy
import json
from functools import wraps
from pathlib import Path

import yaml
from pymatgen.core import Structure as pmg_Structure
from pymatgen_diffusion.neb.pathfinder import IDPPSolver

from common.error import XSDFileNotFoundError, TooManyXSDFileError
from common.file import POSCAR, OUTCAR, ARCFile, INCAR, XSDFile, KPOINTS, POTCAR, XDATCAR, CHGCAR, AECCAR0, AECCAR2, \
    CHGCAR_mag
from common.logger import logger, root_dir
from common.structure import Structure


def write_wrapper(func):
    @wraps(func)
    def wrapper(self):
        func(self)
        self.incar.write(name="INCAR")

    return wrapper


class BaseTask(metaclass=abc.ABCMeta):
    """
    Task Base class, load config.json, generate INCAR, KPOINTS, POSCAR and POTCAR
    Note: subclass should have `generate` method
    """
    with open(f"{root_dir}/config.json", "r") as f:  # TODO: can modify
        CONFIG = json.load(f)

    workdir = Path.cwd()
    config_dir = Path(CONFIG['config_dir'])  # directory of some necessary files (e.g., INCAR, pot, UValue.yaml)
    incar_template = INCAR(config_dir / CONFIG['INCAR'])  # location of incar_template
    potdir = config_dir / CONFIG['potdir']  # location of potdir
    potential = CONFIG['potential']  # potential option: ['PAW_LDA', 'PAW_PBE', 'PAW_PW91', 'USPP_LDA', 'USPP_PW91']

    with open(config_dir / CONFIG['UValue']) as f:
        UValue = yaml.safe_load(f.read())

    def __init__(self):
        self.structure = None
        self.elements = None
        self.incar = self.incar_template

    @abc.abstractmethod
    def generate(self):
        """
        generate main method, subclass should inherit or overwrite
        """
        self._generate_POSCAR()
        self._generate_KPOINTS()
        self._generate_POTCAR()
        self._generate_INCAR()

    def _generate_INCAR(self):
        """
        generate by copy incar_template, modify the +U parameters
        """
        if self.incar.LDAU:
            LDAUL, LDAUU, LDAUJ = [], [], []
            for element in self.elements:
                if self.UValue.get(f'Element {element}', None) is not None:
                    LDAUL.append(self.UValue[f'Element {element}']['orbital'])
                    LDAUU.append(self.UValue[f'Element {element}']['U'])
                    LDAUJ.append(self.UValue[f'Element {element}']['J'])
                else:
                    LDAUL.append(-1)
                    LDAUU.append(0.0)
                    LDAUJ.append(0.0)
                    logger.warning(f"{element} not found in UValue, +U parameters set default: LDAUL = -1, "
                                   f"LDAUU = 0.0, "
                                   f"LDAUJ = 0.0")

            self.incar.LDAUL = LDAUL
            self.incar.LDAUU = LDAUU
            self.incar.LDAUJ = LDAUJ
            self.incar.LMAXMIX = 6 if 3 in self.incar.LDAUL else 4

    def _generate_KPOINTS(self):
        """
        generate KPOINTS, Gamma-centered mesh, number is autogenerated
        """
        with open("KPOINTS", "w") as f:
            f.write("AutoGenerated \n")
            f.write("0 \n")
            f.write("Gamma \n")
            f.write(f"{' '.join(list(map(str, KPOINTS.min_number(lattice=self.structure.lattice))))} \n")
            f.write("0 0 0 \n")

    def _generate_POSCAR(self, *args, **kargs):
        """
        generate POSCAR from only one *.xsd file, and register `self.structure` and `self.elements`
        """
        xsdFiles = list(self.workdir.glob("*.xsd"))
        if not len(xsdFiles):
            raise XSDFileNotFoundError("*.xsd file is not found, please check workdir")
        elif len(xsdFiles) > 1:
            raise TooManyXSDFileError("exist more than one *.xsd file, please check workdir")

        xsdfile = XSDFile(xsdFiles[0])
        self.structure = xsdfile.structure
        self.elements = list(self.structure.atoms.size.keys())
        self.structure.write_POSCAR(name="POSCAR")

    def _generate_POTCAR(self):
        """
         generate POTCAR automatically, call the `cat` method of POTCAR
         """
        potcar = POTCAR.cat(potentials=self.potential, elements=self.elements, potdir=self.potdir)
        potcar.write(name="POTCAR")


class Animatable(metaclass=abc.ABCMeta):

    @staticmethod
    @abc.abstractmethod
    def movie(name):
        """
        make *.arc file to visualize the optimization steps
        """
        XDATCAR("XDATCAR").movie(name=name)


class OptTask(BaseTask, Animatable):
    """
    Optimization task manager, subclass of BaseTask
    """

    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(OptTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, but add wrapper to write INCAR
        """
        super(OptTask, self)._generate_INCAR()

    @staticmethod
    def movie(name="movie.arc"):
        super().movie(name=name)


class ChargeTask(BaseTask):
    """
    Charge calculation task manager, subclass of BaseTask
    """

    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(ChargeTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 1
            LAECHG = .TRUE.
            LCHARG = .TRUE.
        """
        super(ChargeTask, self)._generate_INCAR()
        self.incar.IBRION = 1
        self.incar.LAECHG = True
        self.incar.LCHARG = True

    @staticmethod
    def split():
        """
        split CHGCAR to CHGCAR_tot && CHGCAR_mag
        """
        CHGCAR("CHGCAR").split()

    @staticmethod
    def sum():
        """
        sum AECCAR0 and AECCAR2 to CHGCAR_sum
        """
        aeccar0 = AECCAR0("AECCAR0")
        aeccar2 = AECCAR2("AECCAR2")
        chgcar_sum = aeccar0 + aeccar2
        chgcar_sum.write()

    @staticmethod
    def to_grd(name="vasp.grd", Dencut=250):
        """
        transform CHGCAR_mag to *.grd file
        """
        CHGCAR_mag("CHGCAR_mag").to_grd(name=name, DenCut=Dencut)


class DOSTask(BaseTask):
    """
    Density of States (DOS) calculation task manager, subclass of BaseTask
    """

    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(DOSTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            ISTART = 1
            ICHARG = 11
            IBRION = -1
            NSW = 1
            LORBIT = 12
            NEDOS = 2000
        """
        super(DOSTask, self)._generate_INCAR()
        self.incar.ISTART = 1
        self.incar.ICHARG = 11
        self.incar.IBRION = -1
        self.incar.NSW = 1
        self.incar.LORBIT = 12
        self.incar.NEDOS = 2000


class FreqTask(BaseTask, Animatable):
    """
    Frequency calculation task manager, subclass of BaseTask
    """

    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(FreqTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 5
            ISYM = 0
            NSW = 1
            NFREE = 2
            POTIM = 0.015
        """
        super(FreqTask, self)._generate_INCAR()
        self.incar.IBRION = 5
        self.incar.ISYM = 0
        self.incar.NSW = 1
        self.incar.NFREE = 2
        self.incar.POTIM = 0.015

    @staticmethod
    def movie(freq="image"):
        """
        visualize the frequency vibration, default: image
        """
        outcar = OUTCAR("OUTCAR")
        outcar.animation_freq(freq=freq)


class MDTask(BaseTask):
    """
     ab-initio molecular dynamics (AIMD) calculation task manager, subclass of BaseTask
     """

    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(MDTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 0
            NSW = 100000
            POTIM = 0.5
            SMASS = 2.
            MDALGO = 2
            TEBEG = 300.
            TEEND = 300.
        """
        super(MDTask, self)._generate_INCAR()
        self.incar.IBRION = 0
        self.incar.NSW = 100000
        self.incar.POTIM = 0.5
        self.incar.SMASS = 2.
        self.incar.MDALGO = 2
        self.incar.TEBEG = 300.
        self.incar.TEEND = 300.


class STMTask(BaseTask):
    """
     Scanning Tunneling Microscope (STM) image modelling calculation task manager, subclass of BaseTask
     """

    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(STMTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            ISTART = 1
            IBRION = -1
            NSW = 0
            LPARD = .TRUE.
            NBMOD = -3
            EINT = 5.
            LSEPB = .FALSE.
            LSEPK = .FALSE.
        """
        super(STMTask, self)._generate_INCAR()
        self.incar.ISTART = 1
        self.incar.IBRION = -1
        self.incar.NSW = 0
        self.incar.LPARD = True
        self.incar.NBMOD = -3
        self.incar.EINT = 5.
        self.incar.LSEPB = False
        self.incar.LSEPK = False


class ConTSTask(BaseTask, Animatable):
    """
     Constrain transition state (Con-TS) calculation task manager, subclass of BaseTask
     """

    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(ConTSTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 1
        """
        super(ConTSTask, self)._generate_INCAR()
        self.incar.IBRION = 1

    @staticmethod
    def movie(name="movie.arc"):
        super().movie(name=name)


class NEBTask(BaseTask, Animatable):
    """
     Nudged Elastic Band (NEB) calculation (no-climbing) task manager, subclass of BaseTask
     """

    def __init__(self, ini_POSCAR=None, fni_POSCAR=None, images=4):
        super(NEBTask, self).__init__()

        self.ini_POSCAR = ini_POSCAR
        self.fni_POSCAR = fni_POSCAR
        self.images = images

        self.structure = POSCAR(self.ini_POSCAR).structure
        self.elements = list(self.structure.atoms.size.keys())

    def generate(self, method="linear", check_overlap=True):
        """
        Overwrite BaseTask's generate, add `method` and `check_overlap` parameters
        """
        self._generate_POSCAR(method=method, check_overlap=check_overlap)
        self._generate_KPOINTS()
        self._generate_POTCAR()
        self._generate_INCAR()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 3
            POTIM = 0.
            SPRING = -5.
            LCLIMB = .FALSE.
            ICHAIN = 0
            IOPT = 3
            MAXMOVE = 0.03
            IMAGES =
        """
        super(NEBTask, self)._generate_INCAR()
        self.incar.IBRION = 3
        self.incar.POTIM = 0.
        self.incar.SPRING = -5.
        self.incar.LCLIMB = False
        self.incar.ICHAIN = 0
        self.incar.IOPT = 3
        self.incar.MAXMOVE = 0.03
        self.incar.IMAGES = self.images

    def _generate_POSCAR(self, method, check_overlap):
        """
        Generate NEB-task images && check their structure overlap
        """
        if method == "idpp":
            self._generate_idpp()
        elif method == "linear":
            self._generate_liner()
        else:
            raise NotImplementedError(f"{method} has not been implemented for NEB task")

        if check_overlap:
            NEBTask._check_overlap()

    def _generate_idpp(self):
        """
        Generate NEB-task images by idpp method (J. Chem. Phys. 140, 214106 (2014))
        """
        ini_structure = pmg_Structure.from_file(self.ini_POSCAR, False)
        fni_structure = pmg_Structure.from_file(self.fni_POSCAR, False)
        obj = IDPPSolver.from_endpoints(endpoints=[ini_structure, fni_structure], nimages=self.images, sort_tol=1.0)
        path = obj.run(maxiter=5000, tol=1e-5, gtol=1e-3, step_size=0.05, max_disp=0.05, spring_const=5.0)

        for image in range(len(path)):
            image_dir = f"{image:02d}"
            Path(f"{image_dir}").mkdir(exist_ok=True)
            POSCAR_file = f"{image_dir}/POSCAR"
            path[image].to(fmt="poscar", filename=POSCAR_file)
        logger.info("Improved interpolation of NEB initial guess has been generated.")

    def _generate_liner(self):
        """
        Generate NEB-task images by linear interpolation method
        """
        ini_structure = POSCAR(self.ini_POSCAR).structure
        fni_structure = POSCAR(self.fni_POSCAR).structure
        assert ini_structure == fni_structure, f"{self.ini_POSCAR} and {self.fni_POSCAR} are not structure match"
        diff_image = (fni_structure - ini_structure) / (self.images + 1)

        # write ini-structure
        ini_dir = f"{00:02d}"
        Path(ini_dir).mkdir(exist_ok=True)
        ini_structure.write_POSCAR(f"{ini_dir}/POSCAR")

        # write fni-structure
        fni_dir = f"{self.images + 1:02d}"
        Path(fni_dir).mkdir(exist_ok=True)
        fni_structure.write_POSCAR(f"{fni_dir}/POSCAR")

        # resolve and write image-structures
        for image in range(self.images):
            image_dir = f"{image + 1:02d}"
            Path(image_dir).mkdir(exist_ok=True)
            image_atoms = copy.deepcopy(ini_structure.atoms)
            image_atoms.frac_coord = [None] * len(image_atoms)
            image_atoms.cart_coord = ini_structure.atoms.cart_coord + diff_image * (image + 1)
            image_atoms.set_coord(ini_structure.lattice)
            image_structure = Structure(atoms=image_atoms, lattice=ini_structure.lattice)
            image_structure.write_POSCAR(f"{image_dir}/POSCAR")
        logger.info("Linear interpolation of NEB initial guess has been generated.")

    @staticmethod
    def _search_neb_dir():
        """
        Search neb task directories from workdir
        """
        neb_dirs = []

        for dir in NEBTask.workdir.iterdir():
            if Path(dir).is_dir() and Path(dir).stem.isdigit():
                neb_dirs.append(dir)
        return neb_dirs

    @staticmethod
    def _check_overlap():
        """
        Check if two atoms' distance is too small, (following may add method to tailor their distances)
        """
        logger.info("Check structures overlap")
        neb_dirs = NEBTask._search_neb_dir()

        for image in neb_dirs:
            structure = POSCAR(Path(f"{image}/POSCAR")).structure
            logger.info(f"check {image.stem} dir...")
            structure.check_overlap()

        logger.info("All structures don't have overlap")

    @staticmethod
    def monitor():
        """
        Monitor tangent, energy and barrier in the NEB-task
        """
        neb_dirs = NEBTask._search_neb_dir()
        ini_energy = 0.
        print("image   tangent          energy       barrier")
        for image in neb_dirs:
            outcar = OUTCAR(f"{image}/OUTCAR")
            if not int(image.stem):
                ini_energy = outcar.last_energy
            barrier = outcar.last_energy - ini_energy
            print(f" {image.stem} \t {outcar.last_tangent:>10.6f} \t {outcar.last_energy} \t {barrier:.6f}")

    @staticmethod
    def movie(name="movie.arc", file="CONTCAR"):
        """
        Generate *.arc file from images/[POSCAR|CONTCAR] files
        """
        neb_dirs = NEBTask._search_neb_dir()
        structures = []

        for image in neb_dirs:
            posfile = "CONTCAR" if file == "CONTCAR" and Path(f"{image}/CONTCAR").exists() else "POSCAR"
            structures.append(POSCAR(f"{image}/{posfile}").structure)

        ARCFile.write(name=name, structure=structures, lattice=structures[0].lattice)


class DimerTask(BaseTask):
    def generate(self):
        """
        fully inherit BaseTask's generate
        """
        super(DimerTask, self).generate()

    @write_wrapper
    def _generate_INCAR(self):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 3
            POTIM = 0.
            ISYM = 0
            ICHAIN = 2
            DdR = 0.005
            DRotMax = 10
            DFNMax = 1.
            DFNMin = 0.01
            IOPT = 2     
        """
        super(DimerTask, self)._generate_INCAR()
        self.incar.IBRION = 3
        self.incar.POTIM = 0.
        self.incar.ISYM = 0
        self.incar.ICHAIN = 2
        self.incar.DdR = 0.005
        self.incar.DRotMax = 10
        self.incar.DFNMax = 1.
        self.incar.DFNMin = 0.01
        self.incar.IOPT = 2
