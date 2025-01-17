import dataclasses
import logging
import re
import docutils.nodes
import networkx
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import (
    cast,
    Callable,
    Dict,
    Generic,
    Optional,
    TypeVar,
    Tuple,
    Iterator,
    Sequence,
    List,
    Union,
    Match,
    Set,
)
from ..flutter import checked
from ..types import Diagnostic, Page, EmbeddedRstParser, SerializableType, ProjectConfig
from .. import util

_T = TypeVar("_T", str, object)
PAT_SUBSTITUTION = re.compile(r"\{\{([\w-]+)\}\}")
logger = logging.getLogger(__name__)


def substitute_text(
    text: str, replacements: Dict[str, str], diagnostics: List[Diagnostic]
) -> str:
    """Apply Giza-style replacements to a string. Report a diagnostic for unknown
       substitutions, and insert an empty string."""

    def substitute(match: Match[str]) -> str:
        """Handle a substitution match."""
        try:
            return replacements[match.group(1)]
        except KeyError:
            diagnostics.append(
                Diagnostic.warning(
                    f'Unknown substitution: "{match.group(1)}". '
                    + "You may intend this substitution to be empty",
                    1,
                )
            )
            return ""

    return PAT_SUBSTITUTION.sub(substitute, text)


def substitute(
    obj: _T, replacements: Dict[str, str], diagnostics: List[Diagnostic]
) -> _T:
    """Apply Giza-style replacements to a Giza node."""
    if isinstance(obj, str):
        return substitute_text(obj, replacements, diagnostics)

    if not dataclasses.is_dataclass(obj):
        return obj

    changes: Dict[str, object] = {}
    for obj_field in dataclasses.fields(obj):
        value = getattr(obj, obj_field.name)
        if isinstance(value, str):
            new_str = substitute_text(value, replacements, diagnostics)
            if new_str is not value:
                changes[obj_field.name] = new_str
        elif dataclasses.is_dataclass(value):
            new_value = substitute(value, replacements, diagnostics)
            if new_value is not value:
                changes[obj_field.name] = new_value

    return dataclasses.replace(obj, **changes) if changes else obj


class Node:
    """A base Giza node."""

    @property
    def line(self) -> int:
        return cast(int, getattr(self, "_start_line", 0))


@checked
@dataclass
class Inherit(Node):
    """A Giza node mixin specifies a parent node."""

    file: str
    ref: str


@dataclass
class Inheritable(Node):
    """A mixin for inheritable Giza nodes."""

    ref: Optional[str]
    replacement: Optional[Dict[str, str]]

    source: Optional[Inherit]
    inherit: Optional[Inherit]


_I = TypeVar("_I", bound=Inheritable)


def inherit(
    project_config: ProjectConfig,
    obj: _I,
    parent: Optional[_I],
    diagnostics: List[Diagnostic],
) -> _I:
    """Implement inheritance on a pair of Giza nodes: parent's fields overwrite any
       unset fields in obj, and substitution variables are replaced if obj is not
       a base node. If parent is None, then only substitution occurs."""
    logger.debug("Inheriting %s", obj.ref)
    changes: Dict[str, object] = {}

    # Inherit replacements
    replacement = obj.replacement.copy() if obj.replacement is not None else {}
    changes["replacement"] = replacement
    if parent is not None and parent.replacement is not None:
        for src, dest in parent.replacement.items():
            if src not in replacement:
                replacement[src] = dest

    # Merge in project-wide constants into the giza substitutions system
    new_replacement = {k: str(v) for k, v in project_config.constants.items()}
    new_replacement.update(replacement)
    replacement = new_replacement

    # Inherit root-level keys
    for field_name in (
        field.name
        for field in dataclasses.fields(obj)
        if field.name not in {"replacement", "ref", "source", "inherit"}
    ):
        value = getattr(obj, field_name)
        if parent is not None and value is None:
            new_value = getattr(parent, field_name)
            if new_value is not None:
                changes[field_name] = new_value
                value = new_value

        # Avoid substituting if this is a base node.
        if value is not None and obj.ref and not obj.ref.startswith("_"):
            changes[field_name] = substitute(value, replacement, diagnostics)

    return dataclasses.replace(obj, **changes) if changes else obj


@dataclass
class GizaFile(Generic[_I]):
    """A GizaFile represents a single Giza YAML file."""

    __slots__ = ("path", "text", "data")

    path: Path
    text: str
    data: Sequence[_I]


@dataclass
class GizaCategory(Generic[_I]):
    """A GizaCategory stores metadata about a "category" of Giza YAML files. For
       example, "steps", or "apiargs". Each GizaCategory contains all types necessary
       to transform a given path into Pages."""

    project_config: ProjectConfig
    nodes: Dict[str, GizaFile[_I]] = field(default_factory=dict)
    dg: "networkx.DiGraph[str]" = field(default_factory=networkx.DiGraph)

    def parse(
        self, path: Path, text: Optional[str] = None
    ) -> Tuple[Sequence[_I], str, List[Diagnostic]]:
        """Abstract method to parse Giza nodes out of YAML source text."""
        pass

    def to_pages(
        self,
        page_factory: Callable[[], Tuple[Page, EmbeddedRstParser]],
        data: Sequence[_I],
    ) -> List[Page]:
        """Abstract method to generate pages from a given set of Giza nodes."""
        pass

    def add(self, path: Path, text: str, elements: Sequence[_I]) -> None:
        """Add a file with one or more Giza nodes."""
        file_id = path.name
        self.nodes[file_id] = GizaFile(path, text, elements)

        for element in elements:
            inherit = None
            if element.source:
                inherit = element.source
            elif element.inherit:
                inherit = element.inherit

            if not inherit:
                continue

            self.dg.add_edge(file_id, inherit.file)

    def reify(self, obj: _I, diagnostics: List[Diagnostic], refs_set: Set[str]) -> _I:
        """Resolve inheritance and substitution in a single Giza node."""
        parent_identifier = obj.source if obj.source is not None else obj.inherit
        parent: Optional[_I] = None
        if parent_identifier is not None:
            try:
                parent_sequence = self.nodes[parent_identifier.file].data
            except KeyError:
                diagnostics.append(
                    Diagnostic.error(
                        f'No such file "{parent_identifier.file}"',
                        parent_identifier.line,
                    )
                )
                return obj
            try:
                _parent: _I = next(
                    x for x in parent_sequence if x.ref == parent_identifier.ref
                )
                if _parent.ref is None:
                    _parent.ref = ""

                # If the child does not have a ref, inherit it from the parent
                if not obj.ref:
                    obj.ref = _parent.ref
                parent = _parent
            except StopIteration:
                diagnostics.append(
                    Diagnostic.error(f"Failed to inherit {obj.ref}", obj.line)
                )
                logger.debug("Inheritance failed: %s", obj.ref)
                return obj

        if obj.ref is None:
            obj.ref = ""

        obj = inherit(self.project_config, obj, parent, diagnostics)

        # Check if ref already exists within the same file
        if obj.ref in refs_set:
            msg = f"ref {obj.ref} already exists"
            diagnostics.append(Diagnostic.error(msg, obj.line))
        elif obj.ref is not None:
            refs_set.add(obj.ref)

        return obj

    def reify_file_id(
        self, file_id: str, diagnostics: Dict[PurePath, List[Diagnostic]]
    ) -> GizaFile[_I]:
        """Resolve inheritance and substitution in a Giza source file."""
        node = self.nodes[file_id]
        refs: Set[str] = set()
        data = [
            self.reify(el, diagnostics.setdefault(node.path, []), refs)
            for el in node.data
        ]

        return dataclasses.replace(node, data=data)

    def reify_all_files(
        self, diagnostics: Dict[PurePath, List[Diagnostic]]
    ) -> Iterator[Tuple[str, GizaFile[_I]]]:
        """Resolve inheritance and substitution in all source files within this category."""

        refs_dict: Dict[str, Set[str]] = {}

        for file_id, node in self.nodes.items():
            if file_id not in refs_dict:
                refs_dict[file_id] = set()

            data = [
                self.reify(
                    el, diagnostics.setdefault(node.path, []), refs_dict[file_id]
                )
                for el in node.data
            ]
            yield file_id, dataclasses.replace(node, data=data)

    def __len__(self) -> int:
        """Return the number of nodes in this category."""
        return len(self.nodes)

    def __delitem__(self, file_id: str) -> None:
        """Remove a file and any nodes it may have created."""
        self.dg.remove_node(file_id)
        del self.nodes[file_id]


@checked
@dataclass
class OldHeading(Node):
    """Giza at one point supported manually setting the rSt character to use
       for a heading. This node specification defines that format.."""

    character: Optional[str]
    text: str


@dataclass
@checked
class HeadingMixin(Node):
    """A mixin for Giza node specifications which define a heading."""

    title: Union[str, OldHeading, None]
    heading: Union[str, OldHeading, None]
    level: Optional[int]
    optional: Optional[bool]

    def render_heading(
        self, parse_rst: EmbeddedRstParser
    ) -> Sequence[SerializableType]:
        """Return a list of heading node representing this heading node's properties."""
        title = self.title if self.title is not None else self.heading
        if title is None:
            return ()

        heading_text = title.text if isinstance(title, OldHeading) else title

        if self.optional:
            heading_text = "Optional: " + heading_text

        result = parse_rst(heading_text, self.line, True)

        # Generate an anchor ID for this heading. It would be useful for this
        # to be unique, but it's not possible to do so in a repeatable fashion
        # without seeing the whole page, so doing that has to fall to the
        # renderer.
        heading_id = docutils.nodes.make_id(
            "".join(util.ast_get_text(node) for node in result)
        )

        return (
            {
                "type": "heading",
                "position": {"start": {"line": self.line}},
                "children": result,
                "id": heading_id,
            },
        )
