import abc
import copy
import logging
import os
import shutil
from functools import wraps
from pathlib import Path

import numpy as np
import yaml
from seekpath import get_path

from gvasp.common.utils import str_list
from gvasp.common.base import Atom
from gvasp.common.constant import GREEN, YELLOW, RESET, RED
from gvasp.common.error import XSDFileNotFoundError, TooManyXSDFileError, ConstrainError
from gvasp.common.file import POSCAR, OUTCAR, ARCFile, XSDFile, KPOINTS, POTCAR, XDATCAR, CHGCAR, AECCAR0, AECCAR2, \
    CHGCAR_mag, INCAR, SubmitFile, CONTCAR, Fort188File
from gvasp.common.setting import WorkDir, ConfigManager
from gvasp.neb.path import IdppPath, LinearPath

logger = logging.getLogger(__name__)


def write_wrapper(file):
    def inner(func):
        @wraps(func)
        def wrapper(*args, **kargs):
            self = args[0]
            func(self, *args[1:], **kargs)
            if file == "INCAR":
                self.incar.write(name=file)
            elif file == "KPOINTS":
                self.kpoints.write(name=file)
            elif file == "submit.script":
                with open(file, "w") as f:
                    f.write(self.submit.pipe(self.submit.submit2write))

        return wrapper

    return inner


def end_symbol(func):
    @wraps(func)
    def wrapper(*args, **kargs):
        self = args[0]
        func(self, *args[1:], **kargs)
        if kargs.get("print_end", True):
            print(f"------------------------------------------------------------------")

    return wrapper


class BaseTask(metaclass=abc.ABCMeta):
    """
    Task Base class, load config.json, generate INCAR, KPOINTS, POSCAR and POTCAR
    Note: subclass should have `generate` method
    """
    Template, PotDir, Scheduler = ConfigManager().template, ConfigManager().potdir, ConfigManager().scheduler
    UValueBase = ConfigManager().UValue

    def __init__(self):
        self.title = None
        self.structure = None
        self.elements = None
        self.valence = None

        # set INCAR template
        self._incar = self.Template if self._search_suffix(".incar") is None else self._search_suffix(".incar")
        self.incar = INCAR(self._incar)

        # init kpoints
        self.kpoints = KPOINTS.from_strings(["AutoGenerated \n", "0 \n", "Gamma \n", "1 1 1 \n", "0 0 0 \n"])

        # set UValue template
        self.UValuePath = self.UValueBase if self._search_suffix(".uvalue") is None else self._search_suffix(".uvalue")
        with open(self.UValuePath) as f:
            self.UValue = yaml.safe_load(f.read())

        # set submit template
        self._submit = self.Scheduler if self._search_suffix(".submit") is None else self._search_suffix(".submit")
        self.submit = SubmitFile(self._submit)
        self.finish = None

    @staticmethod
    def get_all_parents():
        def get_parent(path: Path):
            parent = path.parent
            if path != parent:
                yield path
                yield from get_parent(parent)
            else:
                yield path

        return [path for path in get_parent(WorkDir.absolute())]

    @staticmethod
    def _search_suffix(suffix):
        """
        Search file with the special suffix in all parents directories

        Args:
            suffix (str): specify the suffix

        Returns:
            file (Path): file path with the special suffix
        """
        for directory in BaseTask.get_all_parents():
            try:
                for file in directory.iterdir():
                    try:
                        if file.is_file() and file.name.endswith(f"{suffix}"):
                            return file
                    except PermissionError:
                        continue
            except PermissionError:
                continue
        else:
            return

    @end_symbol
    @abc.abstractmethod
    def generate(self, potential: (str, list) = "PAW_PBE", continuous: bool = False, low: bool = False,
                 print_end: bool = True, analysis: bool = False, vdw: bool = False, sol: bool = False,
                 gamma: bool = False, mag: bool = False, hse: bool = False, static: bool = False, nelect=None,
                 points: int = 21):
        """
        generate main method, subclass should inherit or overwrite
        """
        if continuous:
            self._generate_cdir(hse=hse)
        self._generate_POSCAR(continuous=continuous)
        self._generate_KPOINTS(low=low, gamma=gamma, points=points)
        self._generate_POTCAR(potential=potential)
        self._generate_INCAR(low=low, vdw=vdw, sol=sol, nelect=nelect, mag=mag, hse=hse, static=static)
        self._generate_fort()
        self._generate_submit(low=low, analysis=analysis, gamma=gamma)
        self._generate_info(potential=potential)
        if low and print_end and self.__class__.__name__ in ['OptTask', 'ConTSTask']:
            print(f"{RED}low first{RESET}")

    def _generate_cdir(self, directory=None, files=None, **kargs):
        Path(directory).mkdir(exist_ok=True)
        for file in files:
            shutil.copy(file, directory)
        os.chdir(directory)
        self.incar = INCAR("INCAR")

    def _generate_info(self, potential):
        """
        generate short information
        """
        print(f"---------------general info (#{self.__class__.__name__})-----------------------")
        print(f"Elements    Total  Relax   potential orbital UValue")
        potential = [potential] * len(self.elements) if isinstance(potential, str) else potential
        index = 0
        for element, p in zip(self.elements, potential):
            element_index = [index for index, formula in enumerate(self.structure.atoms.formula) if formula == element]
            element_tf = np.sum(
                np.sum(self.structure.atoms.selective_matrix[element_index] == ["F", "F", "F"], axis=1) == 3)
            print(f"{element:^10s}"
                  f"{self.structure.atoms.size[element]:>6d}"
                  f"{element_tf:>6d}(F)   "
                  f"{p}    ", end="")
            if self.incar.LHFCALC is None:
                print(f"{self.incar.LDAUL[index]:>2d}     "
                      f"{self.incar.LDAUU[index] - self.incar.LDAUJ[index]}")
            else:
                print(f"{-1:>2d}     "
                      f"{0.0}")
            index += 1
        print()

        if self.__class__.__name__ == "BandTask":
            print(f"KPoints: line-mode for band structure")
        else:
            print(f"KPoints: [{str_list(self.kpoints.number)}]")

        print()
        print(f"{GREEN}Job Name: {self.title}{RESET}")
        print(f"{YELLOW}INCAR template: {self._incar}{RESET}")
        print(f"{YELLOW}UValue template: {self.UValuePath}{RESET}")
        print(f"{YELLOW}Submit template: {self._submit}{RESET}")

        if getattr(self.incar, "IVDW", None) is not None:
            print(f"{RED}--> VDW-correction: IVDW = {self.incar.IVDW}{RESET}")

        if getattr(self.incar, "LSOL", None) is not None:
            print(f"{RED}--> Solvation calculation{RESET}")

        if getattr(self.incar, "NELECT", None) is not None:
            print(f"{RED}--> Charged system, NELECT = {self.incar.NELECT}{RESET}")

        if self.incar.LHFCALC is not None:
            print(f"{RED}--> HSE06 calculation{RESET}")

        if self.kpoints.number == [1, 1, 1]:
            print(f"{RED}--> Gamma-point calculation{RESET}")

        if self.incar.NSW == 1:
            print(f"{RED}--> Static calculation{RESET}")

    def _generate_INCAR(self, vdw=False, sol=False, nelect=None, mag=False, hse=False, static=False, **kargs):
        """
        generate by copy incar_template, modify the +U parameters
        """
        if self.incar.LDAU and not hse:
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

        if vdw:
            self.incar.IVDW = 12

        if sol:
            self.incar.LSOL = True  # only consider water, not set EB_K

        if nelect is not None:
            t_valence = sum([num * valence for (_, num), valence in zip(self.structure.atoms.elements, self.valence)])
            self.incar.NELECT = t_valence + float(nelect)

        if mag:
            try:
                self.incar.MAGMOM = list(self.structure.atoms.spin)
            except TypeError:
                logger.warning("Can't obtain the `MAGMOM` field, setting `MAG calculation` failed")

        if hse:
            self.incar.LHFCALC = True
            self.incar.HFSCREEN = 0.2
            self.incar.TIME = 0.4
            self.incar.PRECFOCK = 'Fast'

        if static:
            self.incar.NSW = 1

    @write_wrapper(file="KPOINTS")
    def _generate_KPOINTS(self, gamma=False, **kargs):
        """
        generate KPOINTS, Gamma-centered mesh, number is autogenerated
        """

        if gamma:
            _number = [1, 1, 1]
        else:
            _number = list(map(str, KPOINTS.min_number(structure=self.structure)))

        self.kpoints.number = _number

    def _generate_POSCAR(self, continuous=False, **kargs):
        """
        generate POSCAR from only one *.xsd file, and register `self.structure` and `self.elements`
        """
        if not continuous:
            CurrentDir = Path.cwd()  # added for pytest
            xsd_files = list(set(list(CurrentDir.glob("*.xsd")) + list(WorkDir.glob("*.xsd"))))
            if not len(xsd_files):
                raise XSDFileNotFoundError("*.xsd file is not found, please check workdir")
            elif len(xsd_files) > 1:
                raise TooManyXSDFileError("exist more than one *.xsd file, please check workdir")

            xsd_file = XSDFile(xsd_files[0])
            self.title = xsd_file.name.stem
            self.structure = xsd_file.structure
            self.elements = list(self.structure.atoms.size.keys())
            self.structure.write_POSCAR(name="POSCAR")
        else:
            self.title = f"continuous-{self.__class__.__name__}"
            self.structure = CONTCAR("CONTCAR").structure
            self.structure.atoms.spin = self.incar.MAGMOM
            self.elements = list(self.structure.atoms.size.keys())
            self.structure.write_POSCAR(name="POSCAR")

    def _generate_POTCAR(self, potential):
        """
         generate POTCAR automatically, call the `cat` method of POTCAR
         """
        potcar = POTCAR.cat(potentials=potential, elements=self.elements, potdir=self.PotDir)
        self.valence = potcar.valence
        potcar.write(name="POTCAR")

    @write_wrapper(file="submit.script")
    def _generate_submit(self, gamma=False, **kargs):
        """
         generate job.submit automatically
         """
        kpoints = getattr(self, '_kpoints', self.kpoints)
        gamma = True if kpoints.number == [1, 1, 1] else gamma

        self.submit.title = self.title
        self.submit.task = self.__class__.__name__.replace("Task", "")
        self.submit.incar = self.incar
        self.submit.kpoints = kpoints
        self.submit = self.submit.build
        self.submit.vasp_line = self.submit.vasp_gam_line if gamma else self.submit.vasp_std_line
        self.submit.submit2write = ['head_lines', '\n', 'env_lines', '\n', 'vasp_line',
                                    'run_line', '\n', 'finish_line']

    def _generate_fort(self):
        """
        Only Valid for Con-TS Task

        """
        pass


class Animatable(metaclass=abc.ABCMeta):

    @staticmethod
    @abc.abstractmethod
    def movie(name):
        """
        Abstractmethod: Generate the *.arc file to be loaded by Material Studio

        Args:
            name (str): the name of the output *.arc file

        """
        pass


class XDATMovie(Animatable):

    def __new__(cls, *args, **kwargs):
        if cls.__name__ == 'XDATMovie':
            raise TypeError(f"<{cls.__name__} class> may not be instantiated")
        return super(XDATMovie, cls).__new__(cls)

    @staticmethod
    def movie(name="movie.arc"):
        """
        make arc file to visualize the optimization steps
        """
        XDATCAR("XDATCAR").movie(name=name)


class NormalTask(BaseTask):

    def __new__(cls, *args, **kwargs):
        if cls.__name__ == 'NormalTask':
            raise TypeError(f"<{cls.__name__} class> may not be instantiated")
        return super(NormalTask, cls).__new__(cls)

    def generate(self, *args, **kargs):
        """
        fully inherit BaseTask's generate
        """
        super(NormalTask, self).generate(*args, **kargs)


class OptTask(NormalTask, XDATMovie):
    """
    Optimization task manager, subclass of NormalTask
    """

    def _generate_cdir(self, directory="opt_cal", files=None, hse=False):
        if files is None:
            files = ["INCAR", "CONTCAR"]

        if hse:
            directory = "hse"
        super(OptTask, self)._generate_cdir(directory=directory, files=files)

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, low=False, **kargs):
        """
        Inherit BaseTask's _generate_INCAR, but add wrapper to write INCAR
        """
        super(OptTask, self)._generate_INCAR(low=low, **kargs)
        self.incar._ENCUT = self.incar.ENCUT
        self.incar._PREC = self.incar.PREC
        if low:
            self.incar.ENCUT = 300.
            self.incar.PREC = "Low"

    @write_wrapper(file="KPOINTS")
    def _generate_KPOINTS(self, low=False, **kargs):
        """
        Inherit BaseTask's _generate_INCAR, but add wrapper to write INCAR
        """
        super(OptTask, self)._generate_KPOINTS(low=False, **kargs)
        self._kpoints = copy.deepcopy(self.kpoints)
        if low:
            self.kpoints.number = [1, 1, 1]

    @write_wrapper(file="submit.script")
    def _generate_submit(self, low=False, **kargs):
        """
         Rewrite NormalTask's _generate_submit
         """
        super(OptTask, self)._generate_submit(low=low, **kargs)

        if low:
            self.submit.submit2write = ['head_lines', '\n', 'env_lines', '\n',
                                        f'#{"/Low Calculation/".center(50, "-")}# \n',
                                        'vasp_gam_line', 'run_line', '\n',
                                        f'#{"/Normal Prepare/".center(50, "-")}# \n',
                                        'check_success_lines', 'backup_lines', 'modify_lines', '\n',
                                        f'#{"/Normal Calculation/".center(50, "-")}# \n',
                                        'vasp_line', 'run_line', '\n',
                                        'finish_line']


class ConTSTask(OptTask, XDATMovie):
    """
     Constrain transition state (Con-TS) calculation task manager, subclass of NormalTask
     """

    def _generate_cdir(self, directory="ts_cal", files=None, **kargs):
        if files is None:
            files = ["INCAR", "CONTCAR", "fort.188"]

        super(ConTSTask, self)._generate_cdir(directory=directory, files=files)

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, low=False, **kargs):
        """
        Inherit OptTask's _generate_INCAR, modify parameters and rewrite to INCAR
        parameters setting:
            IBRION = 1
        """
        super(ConTSTask, self)._generate_INCAR(low=low, **kargs)
        self.incar.IBRION = 1

    def _generate_fort(self):
        """
        Implement <_generate_fort method> => generate the fort.188 file

        """

        constrain_atom = [atom for atom in self.structure.atoms if atom.constrain]
        if len(constrain_atom) != 2:
            raise ConstrainError("Number of constrain atoms should equal to 2")

        distance = Atom.distance(constrain_atom[0], constrain_atom[1], lattice=self.structure.lattice)
        with open("fort.188", "w") as f:
            f.write("1 \n")
            f.write("3 \n")
            f.write("6 \n")
            f.write("3 \n")
            f.write("0.03 \n")
            f.write(f"{constrain_atom[0].order + 1} {constrain_atom[1].order + 1} {distance:.4f}\n")
            f.write("0 \n")

        logger.info(f"Constrain Information: {constrain_atom[0].order + 1}-{constrain_atom[1].order + 1}, "
                    f"distance = {distance:.4f}")

    def _generate_submit(self, low=False, **kargs):
        """
         Add constrain information to OptTask._generate_submit

         """

        fort188 = Fort188File("fort.188")
        self.submit.constrain = fort188.constrain

        super(ConTSTask, self)._generate_submit(low=low, **kargs)


class ChargeTask(NormalTask):
    """
    Charge calculation task manager, subclass of NormalTask
    """

    def _generate_cdir(self, directory="chg_cal", files=None, **kargs):
        if files is None:
            files = ["INCAR", "CONTCAR"]
        super(ChargeTask, self)._generate_cdir(directory=directory, files=files)

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 1
            LAECHG = .TRUE.
            LCHARG = .TRUE.
        """
        super(ChargeTask, self)._generate_INCAR(**kargs)
        self.incar.IBRION = 1
        self.incar.LAECHG = True
        self.incar.LCHARG = True

    @write_wrapper(file="submit.script")
    def _generate_submit(self, analysis=False, gamma=False, low=False):
        """
         generate job.submit automatically
         """
        super(ChargeTask, self)._generate_submit(gamma=gamma)

        if analysis:
            ChargeTask.apply_analysis(self.submit)

    @staticmethod
    def apply_analysis(submit):
        try:
            submit.submit2write.remove('finish_line')
        except ValueError:
            pass

        submit._task, submit.task = submit.task, "Charge"
        _check_success_lines = submit.pipe(['check_success_lines'])
        submit.task = submit._task
        submit.submit2write += [f'#{"/Charge Analysis Calculation/".center(50, "-")}# \n',
                                f'{_check_success_lines}',
                                'bader_lines', '\n', 'spin_lines', '\n', 'finish_line']

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
        transform CHGCAR_mag to grd file
        """
        CHGCAR_mag("CHGCAR_mag").to_grd(name=name, DenCut=Dencut)


class WorkFuncTask(NormalTask):
    """
    Work Function calculation task manager, subclass of NormalTask
    """

    def _generate_cdir(self, directory="workfunc", files=None, hse=False):
        if files is None:
            files = ["INCAR", "CONTCAR"]
        super(WorkFuncTask, self)._generate_cdir(directory=directory, files=files)

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = -1
            NSW = 1
            LVHAR = .TRUE.
        """
        super(WorkFuncTask, self)._generate_INCAR(**kargs)
        self.incar.IBRION = -1
        self.incar.NSW = 1
        self.incar.LVHAR = True


class BandTask(NormalTask):
    """
    Band Structure calculation task manager, subclass of NormalTask
    """

    def _generate_cdir(self, directory="band_cal", files=None, hse=False):
        if files is None:
            files = ["INCAR", "CONTCAR", "CHGCAR"]
        super(BandTask, self)._generate_cdir(directory=directory, files=files)

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
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
        super(BandTask, self)._generate_INCAR(**kargs)
        self.incar.ISTART = 1
        self.incar.ICHARG = 11
        self.incar.IBRION = -1
        self.incar.NSW = 1
        self.incar.LCHARG = False

        if getattr(self.incar, "LAECHG", None) is not None:
            del self.incar.LAECHG

    def _generate_KPOINTS(self, low=False, gamma=False, points=21):
        """
        generate KPOINTS in line-mode
        """
        lattice = self.structure.lattice.matrix
        positions = self.structure.atoms.frac_coord
        numbers = self.structure.atoms.number
        spglib_structure = (lattice, positions, numbers)

        path = get_path(structure=spglib_structure)
        KLabel = path['point_coords']
        KPath = path['path']

        with open("KPOINTS", "w") as f:
            f.write("K Along High Symmetry Lines \n")
            f.write(f"{points} \n")
            f.write(f"Line-Mode \n")
            f.write(f"Rec \n")

            for (start, end) in KPath:
                start_str = [format(item, "8.5f") for item in KLabel[start]]
                end_str = [format(item, "8.5f") for item in KLabel[end]]

                f.write(f"{' '.join(start_str)}\t!{start} \n")
                f.write(f"{' '.join(end_str)}\t!{end} \n")
                f.write("\n")


class DOSTask(NormalTask):
    """
    Density of States (DOS) calculation task manager, subclass of NormalTask
    """

    def _generate_cdir(self, directory="dos_cal", files=None, hse=False):
        if files is None:
            files = ["INCAR", "CONTCAR", "CHGCAR"]
        super(DOSTask, self)._generate_cdir(directory=directory, files=files)

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
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
        super(DOSTask, self)._generate_INCAR(**kargs)
        self.incar.ISTART = 1
        self.incar.ICHARG = 11
        self.incar.IBRION = -1
        self.incar.NSW = 1
        self.incar.LORBIT = 12
        self.incar.NEDOS = 2000
        self.incar.LCHARG = False

        if getattr(self.incar, "LAECHG", None) is not None:
            del self.incar.LAECHG


class FreqTask(NormalTask, Animatable):
    """
    Frequency calculation task manager, subclass of NormalTask
    """

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
        """
        Inherit BaseTask's _generate_INCAR, modify parameters and write to INCAR
        parameters setting:
            IBRION = 5
            ISYM = 0
            NSW = 1
            NFREE = 2
            POTIM = 0.015
        """
        super(FreqTask, self)._generate_INCAR(**kargs)
        self.incar.IBRION = 5
        self.incar.ISYM = 0
        self.incar.NSW = 1
        self.incar.NFREE = 2
        self.incar.POTIM = 0.015

    @staticmethod
    def movie(file="OUTCAR", freq="image"):
        """
        visualize the frequency vibration, default: image
        """
        outcar = OUTCAR(file)
        outcar.animation_freq(freq=freq)


class MDTask(NormalTask, XDATMovie):
    """
     ab-initio molecular dynamics (AIMD) calculation task manager, subclass of NormalTask
     """

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
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
        super(MDTask, self)._generate_INCAR(**kargs)
        self.incar.IBRION = 0
        self.incar.NSW = 100000
        self.incar.POTIM = 0.5
        self.incar.SMASS = 2.
        self.incar.MDALGO = 2
        self.incar.TEBEG = 300.
        self.incar.TEEND = 300.


class STMTask(NormalTask):
    """
     Scanning Tunneling Microscope (STM) image modelling calculation task manager, subclass of NormalTask
     """

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
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
        super(STMTask, self)._generate_INCAR(**kargs)
        self.incar.ISTART = 1
        self.incar.IBRION = -1
        self.incar.NSW = 0
        self.incar.LPARD = True
        self.incar.NBMOD = -3
        self.incar.EINT = 5.
        self.incar.LSEPB = False
        self.incar.LSEPK = False


class NEBTask(BaseTask, Animatable):
    """
     Nudged Elastic Band (NEB) calculation (no-climbing) task manager, subclass of BaseTask
     """

    def __init__(self, ini_poscar=None, fni_poscar=None, images=4):
        super(NEBTask, self).__init__()

        self.ini_poscar = ini_poscar
        self.fni_poscar = fni_poscar
        self.images = images

        self.structure = POSCAR(self.ini_poscar).structure
        self.elements = list(zip(*self.structure.atoms.elements))[0]

    @staticmethod
    def sort(ini_poscar, fni_poscar):
        """
        Tailor the atoms' order for neb task

        @param:
            ini_poscar:   initial POSCAR file name
            fni_poscar:   final POSCAR file name
        """
        POSCAR.align(ini_poscar, fni_poscar)

    @end_symbol
    def generate(self, method="linear", check_overlap=True, potential="PAW_PBE", vdw=False, sol=False, gamma=False,
                 nelect=None, mag=False, hse=False, static=False):
        """
        Overwrite BaseTask's generate, add `method` and `check_overlap` parameters
        """
        self._generate_POSCAR(method=method, check_overlap=check_overlap)
        self._generate_KPOINTS(gamma)
        self._generate_POTCAR(potential=potential)
        self._generate_INCAR(vdw=vdw, sol=sol, nelect=nelect, mag=mag, hse=hse, static=static)
        self._generate_info(potential=potential)

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
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
        super(NEBTask, self)._generate_INCAR(**kargs)
        self.incar.IBRION = 3
        self.incar.POTIM = 0.
        self.incar.SPRING = -5.
        self.incar.LCLIMB = False
        self.incar.ICHAIN = 0
        self.incar.IOPT = 3
        self.incar.MAXMOVE = 0.03
        self.incar.IMAGES = self.images

    def _generate_POSCAR(self, method=None, check_overlap=None, continuous=None):
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

        idpp_path = IdppPath.from_linear(self.ini_poscar, self.fni_poscar, self.images)
        idpp_path.run()
        idpp_path.write()

        logger.info("Improved interpolation of NEB initial guess has been generated.")

    def _generate_liner(self):
        """
        Generate NEB-task images by linear interpolation method
        """

        linear_path = LinearPath(self.ini_poscar, self.fni_poscar, self.images)
        linear_path.run()
        linear_path.write()

        logger.info("Linear interpolation of NEB initial guess has been generated.")

    @staticmethod
    def _search_neb_dir(workdir=None):
        """
        Search neb task directories from workdir
        """
        if workdir is None:
            workdir = WorkDir

        neb_dirs = []

        for directory in workdir.iterdir():
            if Path(directory).is_dir() and Path(directory).stem.isdigit():
                neb_dirs.append(directory)
        return sorted(neb_dirs)

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
    def movie(name="movie.arc", file="CONTCAR", workdir=None):
        """
        Generate arc file from images/[POSCAR|CONTCAR] files
        """
        neb_dirs = NEBTask._search_neb_dir(workdir)
        structures = []

        for image in neb_dirs:
            posfile = "CONTCAR" if file == "CONTCAR" and Path(f"{image}/CONTCAR").exists() else "POSCAR"
            structures.append(POSCAR(f"{image}/{posfile}").structure)

        ARCFile.write(name=name, structure=structures, lattice=structures[0].lattice)


class DimerTask(NormalTask, XDATMovie):

    @write_wrapper(file="INCAR")
    def _generate_INCAR(self, **kargs):
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
        super(DimerTask, self)._generate_INCAR(**kargs)
        self.incar.IBRION = 3
        self.incar.POTIM = 0.
        self.incar.ISYM = 0
        self.incar.ICHAIN = 2
        self.incar.DdR = 0.005
        self.incar.DRotMax = 10
        self.incar.DFNMax = 1.
        self.incar.DFNMin = 0.01
        self.incar.IOPT = 2


class SequentialTask(object):
    """
    Apply Sequential Task from `opt => chg` or `opt => dos`
    """

    def __init__(self, end):
        """
        Args:
            end: specify the end task, optional: [opt, chg, dos]
        """
        self.end = end
        self.submit = None

    @end_symbol
    @write_wrapper(file="submit.script")
    def generate(self, potential="PAW_PBE", low=False, analysis=False, vdw=False, sol=False, gamma=False, nelect=None,
                 hse=False, static=False):
        task = OptTask()
        task.generate(potential=potential, low=low, print_end=False, vdw=vdw, sol=sol, gamma=gamma, nelect=nelect,
                      hse=hse, static=static)
        self.submit = task.submit

        if self.end == "chg" or self.end == "dos":
            low_string = "low first, " if low else ""
            analysis_string = "apply analysis" if analysis else ""
            print(f"{RED}Sequential Task: opt => {self.end}, " + low_string + analysis_string + RESET)
            self.submit.submit2write.remove("finish_line")
            self.submit.submit2write += [f'#{"/Charge Calculation/".center(50, "-")}# \n',
                                         'check_success_lines',
                                         "mkdir chg_cal \n",
                                         "cp OUTCAR OUTCAR_backup \n",
                                         "cp INCAR KPOINTS POTCAR chg_cal \n",
                                         "cp CONTCAR chg_cal/POSCAR \n",
                                         f"sed -i '/IBRION/c\  IBRION = 1' chg_cal/INCAR \n",
                                         f"sed -i '/LCHARG/c\  LCHARG = .TRUE.' chg_cal/INCAR \n",
                                         f"sed -i '/LCHARG/a\  LAECHG = .TRUE.' chg_cal/INCAR \n",
                                         f"cd chg_cal || return \n", '\n',
                                         "vasp_line", 'run_line', '\n', 'finish_line']

            if analysis:
                ChargeTask.apply_analysis(self.submit)

        if self.end == "wf":
            low_string = "low first, " if low else ""
            print(f"{RED}Sequential Task: opt => {self.end}, " + low_string + RESET)
            self.submit.submit2write.remove("finish_line")
            self.submit.submit2write += [f'#{"/WorkFunc Calculation/".center(50, "-")}# \n',
                                         'check_success_lines',
                                         "mkdir workfunc \n",
                                         "cp OUTCAR OUTCAR_backup \n",
                                         "cp INCAR KPOINTS POTCAR workfunc \n",
                                         "cp CONTCAR workfunc/POSCAR \n",
                                         f"sed -i '/IBRION/c\  IBRION = -1' workfunc/INCAR \n",
                                         f"sed -i '/NSW/c\  NSW = 1' workfunc/INCAR \n",
                                         f"sed -i '/NSW/a\  LVHAR = .TRUE.' workfunc/INCAR \n",
                                         f"cd workfunc || return \n", '\n',
                                         "vasp_line", 'run_line', '\n', 'finish_line']

        if self.end == "dos":
            self.submit.submit2write.remove("finish_line")
            self.submit._task, self.submit.task = self.submit.task, "Charge"
            _check_success_lines = self.submit.pipe(['check_success_lines'])
            self.submit.task = self.submit._task
            self.submit.submit2write += [f'#{"/DOS Calculation/".center(50, "-")}# \n',
                                         f'{_check_success_lines}',
                                         "mkdir dos_cal \n",
                                         "cp OUTCAR OUTCAR_backup \n",
                                         "cp INCAR KPOINTS POTCAR CHGCAR dos_cal \n",
                                         "cp CONTCAR dos_cal/POSCAR \n",
                                         f"sed -i '/ISTART/c\  ISTART = 1' dos_cal/INCAR \n",
                                         f"sed -i '/NSW/c\  NSW = 1' dos_cal/INCAR \n",
                                         f"sed -i '/IBRION/c\  IBRION = -1' dos_cal/INCAR \n",
                                         f"sed -i '/LCHARG/c\  LCHARG = .FALSE.' dos_cal/INCAR \n",
                                         f"sed -i '/LCHARG/a\  LAECHG = .FALSE.' dos_cal/INCAR \n",
                                         f"sed -i '/+U/i\  ICHARG = 11' dos_cal/INCAR \n",
                                         f"sed -i '/ICHARG/a\  LORBIT = 12' dos_cal/INCAR \n",
                                         f"sed -i '/ICHARG/a\  NEDOS = 2000' dos_cal/INCAR \n",
                                         f"cd dos_cal || return \n", '\n',
                                         "vasp_line", 'run_line', '\n', 'finish_line']

        if self.end not in ['opt', 'chg', 'wf', 'dos']:
            raise TypeError(f"Unsupported Sequential Task to {self.end}, should be [opt, chg, wf, dos]")


class OutputTask(object):
    @staticmethod
    def output(name):
        """
        Transform the results to .xsd file
        """

        XSDFile.write(contcar="CONTCAR", outcar="OUTCAR", name=name)
