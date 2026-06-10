"""
Onixey · validation/integrity.py
=================================
Validación de integridad estructural entre módulos del sistema runtime.
No ejecuta lógica de animación. No modifica datos. Compatible con F8/hot reload.

Autor  : Arquitecto Senior AAA – Onixey
Versión: 1.0.0
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import sys
import traceback
from dataclasses import dataclass, field
from enum import Enum, auto
from types import ModuleType
from typing import Any, Callable, Dict, Final, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Logger – sin handlers propios para no contaminar el entorno Blender.
# La configuración de formato/nivel la maneja el sistema de logging del
# addon (o el usuario). Compatible con F8/hot reload.
# ---------------------------------------------------------------------------
log: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos de error propios
# ---------------------------------------------------------------------------

class IntegrityErrorKind(Enum):
    NULL_REFERENCE        = auto()
    MISSING_COMPONENT     = auto()
    BROKEN_DEPENDENCY     = auto()
    DUPLICATE_REGISTRY    = auto()
    DEAD_OBJECT           = auto()
    CIRCULAR_IMPORT       = auto()
    CROSS_CONTAMINATION   = auto()
    MODULE_UNAVAILABLE    = auto()
    ATTRIBUTE_MISSING     = auto()
    TYPE_MISMATCH         = auto()


@dataclass(frozen=True)
class IntegrityError:
    kind    : IntegrityErrorKind
    module  : str
    detail  : str
    hint    : str = ""

    def as_str(self) -> str:
        base = f"[{self.kind.name}] {self.module} – {self.detail}"
        return f"{base}  (hint: {self.hint})" if self.hint else base


@dataclass(frozen=True)
class IntegrityWarning:
    module : str
    detail : str

    def as_str(self) -> str:
        return f"{self.module} – {self.detail}"


# ---------------------------------------------------------------------------
# Resultado estructurado
# ---------------------------------------------------------------------------

@dataclass
class IntegrityReport:
    passed  : List[str]             = field(default_factory=list)
    warnings: List[IntegrityWarning] = field(default_factory=list)
    errors  : List[IntegrityError]   = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def health_score(self) -> int:
        """
        Puntuación 0-100.
        Cada error descuenta 15 puntos; cada warning descuenta 5.
        Mínimo 0.
        """
        score = 100 - (len(self.errors) * 15) - (len(self.warnings) * 5)
        return max(0, score)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "health_score": self.health_score,
            "passed"      : list(self.passed),
            "warnings"    : [w.as_str() for w in self.warnings],
            "errors"      : [e.as_str() for e in self.errors],
        }

    # ------------------------------------------------------------------
    def log_summary(self) -> None:
        log.info("══════════════════════════════════════════════")
        log.info(f"  HEALTH SCORE : {self.health_score}/100")
        log.info(f"  ✔ passed     : {len(self.passed)}")
        log.info(f"  ⚠ warnings   : {len(self.warnings)}")
        log.info(f"  ✖ errors     : {len(self.errors)}")
        log.info("══════════════════════════════════════════════")
        for p in self.passed:
            log.debug(f"  ✔  {p}")
        for w in self.warnings:
            log.warning(f"  ⚠  {w.as_str()}")
        for e in self.errors:
            log.error(f"  ✖  {e.as_str()}")


# ---------------------------------------------------------------------------
# Contrato de atributos esperados por módulo
# ---------------------------------------------------------------------------

#  Formato:  module_path -> [(attr_name, expected_type_or_None)]
#
#  Solo se listan exportaciones que EXISTEN en el código real de cada módulo.
#  Verificado contra:
#    runtime/registry.py   → register_component, get_component, _ComponentRegistry
#    runtime/state.py      → RuntimeStateManager, get_state, is_active
#    runtime/event_bus.py  → subscribe, emit, EventName
#    runtime/cache.py      → invalidate_all, invalidate_l1
#    runtime/session.py    → initialize, get
#    runtime/handlers.py   → startup, shutdown, is_started
EXPECTED_ATTRIBUTES: Final[Dict[str, List[Tuple[str, Optional[type]]]]] = {
    "runtime.registry": [
        ("register_component",  None),
        ("get_component",       None),
        ("_ComponentRegistry",  None),
    ],
    "runtime.state": [
        ("RuntimeStateManager", None),
        ("get_state",           None),
        ("is_active",           None),
    ],
    "runtime.event_bus": [
        ("subscribe",           None),
        ("emit",                None),
        ("EventName",           None),
    ],
    "runtime.cache": [
        ("invalidate_all",      None),
        ("invalidate_l1",       None),
    ],
    "runtime.session": [
        ("initialize",          None),
        ("get",                 None),
    ],
    "runtime.handlers": [
        ("startup",             None),
        ("shutdown",            None),
        ("is_started",          None),
    ],
}

# Atributos de módulo que NO deben cruzar fronteras de sistema
ISOLATION_RULES: Final[Dict[str, Set[str]]] = {
    "runtime.state"    : {"runtime.event_bus", "runtime.context"},
    "runtime.cache"    : {"runtime.registry"},
    "runtime.handlers" : {"runtime.session", "runtime.state"},
}

# Nombres de módulos prohibidos en imports de runtime (contaminación)
FORBIDDEN_IMPORTS: Final[Set[str]] = {
    "bpy.ops",
    "bpy.types.FCurves",
    "mathutils",          # advertencia, no error
}
FORBIDDEN_IMPORTS_HARD: Final[Set[str]] = {"bpy.ops"}


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------

def _safe_import(module_path: str) -> Tuple[Optional[ModuleType], Optional[str]]:
    """
    Intenta importar un módulo sin propagar excepciones.
    Devuelve (module, None) o (None, error_message).
    """
    try:
        mod = importlib.import_module(module_path)
        return mod, None
    except ModuleNotFoundError as exc:
        return None, f"ModuleNotFoundError: {exc}"
    except ImportError as exc:
        return None, f"ImportError: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"UnexpectedError: {exc}"


def _get_attr_safe(obj: Any, attr: str) -> Tuple[bool, Any, Optional[str]]:
    """
    Obtiene un atributo de forma segura.
    Retorna (found, value, error_or_None).
    """
    try:
        val = getattr(obj, attr)
        return True, val, None
    except AttributeError:
        return False, None, f"AttributeError: '{attr}' no existe"
    except Exception as exc:  # noqa: BLE001
        return False, None, str(exc)


def _detect_dead_object(obj: Any) -> bool:
    """
    Heurística para detectar objetos 'muertos' de Blender
    (bpy wrappers inválidos) sin ejecutar lógica.
    """
    # Los objetos bpy invalidados lanzan ReferenceError al acceder a .name
    try:
        if hasattr(obj, "name") and callable(getattr(obj, "name", None)):
            return False
        _ = obj.name  # type: ignore[union-attr]
        return False
    except ReferenceError:
        return True
    except Exception:  # noqa: BLE001
        return False


def _collect_imports(mod: ModuleType) -> Set[str]:
    """
    Extrae los nombres de módulos importados desde el source del módulo
    usando ``ast``. Más fiable que el parser de texto manual:

    - Captura ``import x``, ``import x.y``, ``from x import …``.
    - Captura imports multilínea y con alias (``import x as y``).
    - No ejecuta ningún código del módulo.
    - Cae en el parser de texto como fallback si el source no está disponible.

    Retorna un set de cadenas con la ruta de módulo completa
    (e.g. ``{"runtime.cache", "threading", "weakref"}``).
    """
    imports: Set[str] = set()

    # Intento 1: usar ast para parsear el source — más robusto.
    source: Optional[str] = None
    try:
        source = inspect.getsource(mod)
    except (OSError, TypeError):
        pass

    if source is not None:
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name)          # e.g. "runtime.cache"
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.add(node.module)          # e.g. "runtime.cache"
            return imports
        except SyntaxError:
            # Fallback al parser de texto si el AST falla (código malformado).
            imports.clear()

    # Intento 2: fallback — parser de texto sobre el source ya obtenido.
    if source is not None:
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import "):
                parts = stripped.split()
                if len(parts) >= 2:
                    # Tomar la ruta completa (e.g. "runtime.cache.helpers").
                    imports.add(parts[1].rstrip(","))
            elif stripped.startswith("from "):
                parts = stripped.split()
                if len(parts) >= 2:
                    imports.add(parts[1])

    # Intento 3: si no hay source, usar sys.modules como proxy conservador.
    # Detecta únicamente módulos que ya están en caché y cuyo nombre empieza
    # con los mismos prefijos que el módulo inspeccionado.
    if not source:
        mod_prefix = getattr(mod, "__package__", None) or ""
        if mod_prefix:
            for key in sys.modules:
                if key != mod.__name__ and key.startswith(mod_prefix):
                    imports.add(key)

    return imports


# ---------------------------------------------------------------------------
# Checkers individuales
# ---------------------------------------------------------------------------

class _IntegrityChecker:
    """
    Cada método `check_*` es independiente: si falla no interrumpe los demás.
    Escribe en el IntegrityReport acumulado.
    """

    def __init__(self, report: IntegrityReport) -> None:
        self._report  = report
        self._modules : Dict[str, ModuleType] = {}

    # ------------------------------------------------------------------
    # 1. Existencia y carga de módulos
    # ------------------------------------------------------------------
    def check_module_existence(self) -> None:
        log.info("▶ [1/7] Verificando existencia de módulos runtime …")
        for mod_path in EXPECTED_ATTRIBUTES:
            mod, err = _safe_import(mod_path)
            if mod is None:
                self._report.errors.append(IntegrityError(
                    kind   = IntegrityErrorKind.MODULE_UNAVAILABLE,
                    module = mod_path,
                    detail = err or "módulo no disponible",
                    hint   = "Verifica que el paquete runtime esté en sys.path",
                ))
                log.error(f"  ✖ {mod_path} → {err}")
            else:
                self._modules[mod_path] = mod
                self._report.passed.append(f"módulo '{mod_path}' importado correctamente")
                log.debug(f"  ✔ {mod_path}")

    # ------------------------------------------------------------------
    # 2. Atributos esperados y referencias nulas
    # ------------------------------------------------------------------
    def check_attributes_and_nulls(self) -> None:
        log.info("▶ [2/7] Verificando atributos y referencias nulas …")
        for mod_path, attrs in EXPECTED_ATTRIBUTES.items():
            mod = self._modules.get(mod_path)
            if mod is None:
                continue  # ya reportado en check_module_existence

            for attr_name, expected_type in attrs:
                found, value, err = _get_attr_safe(mod, attr_name)
                if not found:
                    self._report.errors.append(IntegrityError(
                        kind   = IntegrityErrorKind.ATTRIBUTE_MISSING,
                        module = mod_path,
                        detail = f"atributo '{attr_name}' no encontrado",
                        hint   = "Puede ser un módulo desactualizado post hot-reload",
                    ))
                    log.error(f"  ✖ {mod_path}.{attr_name} → {err}")
                    continue

                # Referencia nula
                if value is None:
                    self._report.errors.append(IntegrityError(
                        kind   = IntegrityErrorKind.NULL_REFERENCE,
                        module = mod_path,
                        detail = f"'{attr_name}' es None",
                        hint   = "El módulo puede no haber sido inicializado",
                    ))
                    log.error(f"  ✖ {mod_path}.{attr_name} = None")
                    continue

                # Tipo incorrecto (si se especificó)
                if expected_type is not None and not isinstance(value, expected_type):
                    self._report.warnings.append(IntegrityWarning(
                        module = mod_path,
                        detail = (
                            f"'{attr_name}' esperaba {expected_type.__name__}, "
                            f"encontrado {type(value).__name__}"
                        ),
                    ))
                    log.warning(f"  ⚠ {mod_path}.{attr_name} tipo incorrecto")
                    continue

                self._report.passed.append(f"{mod_path}.{attr_name} OK")
                log.debug(f"  ✔ {mod_path}.{attr_name}")

    # ------------------------------------------------------------------
    # 3. Objetos muertos (referencias Blender invalidadas)
    # ------------------------------------------------------------------
    def check_dead_objects(self) -> None:
        log.info("▶ [3/7] Verificando objetos muertos (bpy wrappers) …")
        for mod_path, mod in self._modules.items():
            try:
                members = inspect.getmembers(mod)
            except Exception as exc:  # noqa: BLE001
                self._report.warnings.append(IntegrityWarning(
                    module = mod_path,
                    detail = f"No se pudo inspeccionar miembros: {exc}",
                ))
                continue

            for name, obj in members:
                if name.startswith("_"):
                    continue
                if _detect_dead_object(obj):
                    self._report.errors.append(IntegrityError(
                        kind   = IntegrityErrorKind.DEAD_OBJECT,
                        module = mod_path,
                        detail = f"objeto '{name}' es un bpy wrapper invalidado",
                        hint   = "Recargar el archivo .blend o reiniciar el sistema",
                    ))
                    log.error(f"  ✖ {mod_path}.{name} → objeto muerto")

    # ------------------------------------------------------------------
    # 4. Registros duplicados en runtime.registry
    # ------------------------------------------------------------------
    def check_duplicate_registry(self) -> None:
        log.info("▶ [4/7] Verificando duplicados en runtime.registry …")
        mod = self._modules.get("runtime.registry")
        if mod is None:
            return

        found, registry_obj, _ = _get_attr_safe(mod, "_ComponentRegistry")
        if not found or registry_obj is None:
            return

        # Intentar acceder a un atributo interno que liste claves
        for candidate in ("_registry", "_store", "_map", "_entries", "__dict__"):
            found_inner, inner, _ = _get_attr_safe(registry_obj, candidate)
            if not found_inner or not isinstance(inner, dict):
                continue
            keys = list(inner.keys())
            duplicates = {k for k in keys if keys.count(k) > 1}
            if duplicates:
                for dup in duplicates:
                    self._report.errors.append(IntegrityError(
                        kind   = IntegrityErrorKind.DUPLICATE_REGISTRY,
                        module = "runtime.registry",
                        detail = f"clave duplicada en Registry: '{dup}'",
                        hint   = "Revisar orden de registro durante la inicialización",
                    ))
                    log.error(f"  ✖ registry duplicado: '{dup}'")
            else:
                self._report.passed.append("runtime.registry sin duplicados detectados")
                log.debug("  ✔ runtime.registry sin duplicados")
            break  # solo necesitamos el primer dict válido

    # ------------------------------------------------------------------
    # 5. Imports circulares detectables
    # ------------------------------------------------------------------
    def check_circular_imports(self) -> None:
        log.info("▶ [5/7] Verificando imports circulares detectables …")
        runtime_names = set(EXPECTED_ATTRIBUTES.keys())

        for mod_path, mod in self._modules.items():
            imported = _collect_imports(mod)
            cycle_candidates = imported & runtime_names - {mod_path}

            for dep in cycle_candidates:
                dep_mod = self._modules.get(dep)
                if dep_mod is None:
                    continue
                dep_imports = _collect_imports(dep_mod)
                if mod_path in dep_imports:
                    self._report.errors.append(IntegrityError(
                        kind   = IntegrityErrorKind.CIRCULAR_IMPORT,
                        module = mod_path,
                        detail = f"import circular detectado: {mod_path} ↔ {dep}",
                        hint   = "Usar importación diferida (TYPE_CHECKING guard)",
                    ))
                    log.error(f"  ✖ circular: {mod_path} ↔ {dep}")

        self._report.passed.append("análisis de imports circulares completado")

    # ------------------------------------------------------------------
    # 6. Imports prohibidos (contaminación de UI / bpy.ops)
    # ------------------------------------------------------------------
    def check_forbidden_imports(self) -> None:
        log.info("▶ [6/7] Verificando imports prohibidos …")
        for mod_path, mod in self._modules.items():
            imported = _collect_imports(mod)
            for forbidden in FORBIDDEN_IMPORTS_HARD:
                if any(i.startswith(forbidden) for i in imported):
                    self._report.errors.append(IntegrityError(
                        kind   = IntegrityErrorKind.CROSS_CONTAMINATION,
                        module = mod_path,
                        detail = f"import prohibido encontrado: '{forbidden}'",
                        hint   = "Los módulos runtime no deben depender de bpy.ops",
                    ))
                    log.error(f"  ✖ {mod_path} importa '{forbidden}'")

            for soft_forbidden in FORBIDDEN_IMPORTS - FORBIDDEN_IMPORTS_HARD:
                if any(i.startswith(soft_forbidden) for i in imported):
                    self._report.warnings.append(IntegrityWarning(
                        module = mod_path,
                        detail = f"import sensible encontrado: '{soft_forbidden}'",
                    ))
                    log.warning(f"  ⚠ {mod_path} importa '{soft_forbidden}'")

        self._report.passed.append("análisis de imports prohibidos completado")

    # ------------------------------------------------------------------
    # 7. Contaminación entre sistemas (isolation rules)
    # ------------------------------------------------------------------
    def check_cross_contamination(self) -> None:
        log.info("▶ [7/7] Verificando contaminación entre sistemas …")
        for mod_path, forbidden_deps in ISOLATION_RULES.items():
            mod = self._modules.get(mod_path)
            if mod is None:
                continue
            imported = _collect_imports(mod)
            for dep in forbidden_deps:
                if dep in imported:
                    self._report.errors.append(IntegrityError(
                        kind   = IntegrityErrorKind.CROSS_CONTAMINATION,
                        module = mod_path,
                        detail = (
                            f"'{mod_path}' importa directamente '{dep}' "
                            f"violando reglas de aislamiento"
                        ),
                        hint   = "Usar inyección de dependencias o el EventBus",
                    ))
                    log.error(f"  ✖ contaminación: {mod_path} → {dep}")

        self._report.passed.append("análisis de contaminación entre sistemas completado")


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def run_integrity_check() -> Dict[str, Any]:
    """
    Punto de entrada principal.
    Ejecuta todos los checks de forma aislada y devuelve el reporte como dict.

    Compatible con Blender F8 / hot reload: no guarda estado global,
    no registra operadores, no modifica datos.

    Returns
    -------
    dict con claves: health_score, passed, warnings, errors
    """
    log.info("════════════════════════════════════════════════")
    log.info("  ONIXEY · Integrity Check iniciado")
    log.info("════════════════════════════════════════════════")

    report  = IntegrityReport()
    checker = _IntegrityChecker(report)

    # Mapa de checks: cada uno es independiente
    checks: List[Tuple[str, Callable[[], None]]] = [
        ("module_existence",      checker.check_module_existence),
        ("attributes_and_nulls",  checker.check_attributes_and_nulls),
        ("dead_objects",          checker.check_dead_objects),
        ("duplicate_registry",    checker.check_duplicate_registry),
        ("circular_imports",      checker.check_circular_imports),
        ("forbidden_imports",     checker.check_forbidden_imports),
        ("cross_contamination",   checker.check_cross_contamination),
    ]

    for check_name, check_fn in checks:
        try:
            check_fn()
        except Exception:  # noqa: BLE001 – efecto dominó evitado deliberadamente
            tb = traceback.format_exc()
            report.errors.append(IntegrityError(
                kind   = IntegrityErrorKind.MISSING_COMPONENT,
                module = f"checker.{check_name}",
                detail = "El checker lanzó una excepción inesperada",
                hint   = tb.splitlines()[-1] if tb else "",
            ))
            log.exception(f"  ✖ checker '{check_name}' falló inesperadamente")

    report.log_summary()
    return report.to_dict()


# ---------------------------------------------------------------------------
# Entrada directa (útil para test manual en consola Blender / Python)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    result = run_integrity_check()
    print(json.dumps(result, indent=2, ensure_ascii=False))
