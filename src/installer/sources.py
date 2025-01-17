"""Source of information about a wheel file."""

import os
import posixpath
import stat
import zipfile
from contextlib import contextmanager
from typing import BinaryIO, ClassVar, Iterator, List, Tuple, Type, cast

from installer.records import RecordEntry, parse_record_file
from installer.utils import canonicalize_name, parse_wheel_filename

WheelContentElement = Tuple[Tuple[str, str, str], BinaryIO, bool]


__all__ = ["WheelSource", "WheelFile"]


class WheelSource:
    """Represents an installable wheel.

    This is an abstract class, whose methods have to be implemented by subclasses.
    """

    validation_error: ClassVar[Type[Exception]] = ValueError

    def __init__(self, distribution: str, version: str) -> None:
        """Initialize a WheelSource object.

        :param distribution: distribution name (like ``urllib3``)
        :param version: version associated with the wheel
        """
        super().__init__()
        self.distribution = distribution
        self.version = version

    @property
    def dist_info_dir(self):
        """Name of the dist-info directory."""
        return f"{self.distribution}-{self.version}.dist-info"

    @property
    def data_dir(self):
        """Name of the data directory."""
        return f"{self.distribution}-{self.version}.data"

    @property
    def dist_info_filenames(self) -> List[str]:
        """Get names of all files in the dist-info directory.

        Sample usage/behaviour::

            >>> wheel_source.dist_info_filenames
            ['METADATA', 'WHEEL']
        """
        raise NotImplementedError

    def read_dist_info(self, filename: str) -> str:
        """Get contents, from ``filename`` in the dist-info directory.

        Sample usage/behaviour::

            >>> wheel_source.read_dist_info("METADATA")
            ...

        :param filename: name of the file
        """
        raise NotImplementedError

    def validate_record(self) -> None:
        """Validate ``RECORD`` of the wheel.

        This method should be called before :py:func:`install <installer.install>`
        if validation is required.
        """
        raise NotImplementedError

    def get_contents(self) -> Iterator[WheelContentElement]:
        """Sequential access to all contents of the wheel (including dist-info files).

        This method should return an iterable. Each value from the iterable must be a
        tuple containing 3 elements:

        - record: 3-value tuple, to pass to
          :py:meth:`RecordEntry.from_elements <installer.records.RecordEntry.from_elements>`.
        - stream: An :py:class:`io.BufferedReader` object, providing the contents of the
          file at the location provided by the first element (path).
        - is_executable: A boolean, representing whether the item has an executable bit.

        All paths must be relative to the root of the wheel.

        Sample usage/behaviour::

            >>> iterable = wheel_source.get_contents()
            >>> next(iterable)
            (('pkg/__init__.py', '', '0'), <...>, False)

        This method may be called multiple times. Each iterable returned must
        provide the same content upon reading from a specific file's stream.
        """
        raise NotImplementedError


class _WheelFileValidationError(ValueError):
    """Raised when a wheel file fails validation."""

    def __init__(self, issues: List[str]) -> None:  # noqa: D107
        super().__init__(repr(issues))
        self.issues = issues

    def __repr__(self) -> str:
        return f"WheelFileValidationError(issues={self.issues!r})"


class WheelFile(WheelSource):
    """Implements `WheelSource`, for an existing file from the filesystem.

    Example usage::

        >>> with WheelFile.open("sampleproject-2.0.0-py3-none-any.whl") as source:
        ...     installer.install(source, destination)
    """

    validation_error = _WheelFileValidationError

    def __init__(self, f: zipfile.ZipFile) -> None:
        """Initialize a WheelFile object.

        :param f: An open zipfile, which will stay open as long as this object is used.
        """
        self._zipfile = f
        assert f.filename

        basename = os.path.basename(f.filename)
        parsed_name = parse_wheel_filename(basename)
        super().__init__(
            version=parsed_name.version,
            distribution=parsed_name.distribution,
        )

    @classmethod
    @contextmanager
    def open(cls, path: "os.PathLike[str]") -> Iterator["WheelFile"]:
        """Create a wheelfile from a given path."""
        with zipfile.ZipFile(path) as f:
            yield cls(f)

    @property
    def dist_info_dir(self) -> str:
        """Name of the dist-info directory."""
        if not hasattr(self, "_dist_info_dir"):
            top_level_directories = {
                path.split("/", 1)[0] for path in self._zipfile.namelist()
            }
            dist_infos = [
                name for name in top_level_directories if name.endswith(".dist-info")
            ]

            assert (
                len(dist_infos) == 1
            ), "Wheel doesn't contain exactly one .dist-info directory"
            dist_info_dir = dist_infos[0]

            # NAME-VER.dist-info
            di_dname = dist_info_dir.rsplit("-", 2)[0]
            norm_di_dname = canonicalize_name(di_dname)
            norm_file_dname = canonicalize_name(self.distribution)
            assert (
                norm_di_dname == norm_file_dname
            ), "Wheel .dist-info directory doesn't match wheel filename"

            self._dist_info_dir = dist_info_dir
        return self._dist_info_dir

    @property
    def dist_info_filenames(self) -> List[str]:
        """Get names of all files in the dist-info directory."""
        base = self.dist_info_dir
        return [
            name[len(base) + 1 :]
            for name in self._zipfile.namelist()
            if name[-1:] != "/"
            if base == posixpath.commonprefix([name, base])
        ]

    def read_dist_info(self, filename: str) -> str:
        """Get contents, from ``filename`` in the dist-info directory."""
        path = posixpath.join(self.dist_info_dir, filename)
        return self._zipfile.read(path).decode("utf-8")

    def validate_record(self, validate_file: bool = True) -> None:
        """Validate ``RECORD`` of the wheel.

        This method should be called before :py:func:`install <installer.install>`
        if validation is required.

        File names will always be validated against ``RECORD``.
        If ``validate_file`` is true, every file in the archive will be validated
        against corresponding entry in ``RECORD``.

        :param validate_file: Whether to validate content integrity.
        """
        try:
            record_lines = self.read_dist_info("RECORD").splitlines()
            records = parse_record_file(record_lines)
        except Exception as exc:
            raise _WheelFileValidationError(
                [f"Unable to retrieve `RECORD` from {self._zipfile.filename}: {exc!r}"]
            ) from exc

        record_mapping = {record[0]: record for record in records}
        issues: List[str] = []

        for item in self._zipfile.infolist():
            if item.filename[-1:] == "/":  # looks like a directory
                continue

            record_args = record_mapping.pop(item.filename, None)

            if self.dist_info_dir == posixpath.commonprefix(
                [self.dist_info_dir, item.filename]
            ) and item.filename.split("/")[-1] in ("RECORD.p7s", "RECORD.jws"):
                # both are for digital signatures, and not mentioned in RECORD
                continue

            if record_args is None:
                issues.append(
                    f"In {self._zipfile.filename}, {item.filename} is not mentioned in RECORD"
                )
                continue

            if item.filename == f"{self.dist_info_dir}/RECORD":
                # Skip record file
                continue
            if validate_file:
                record = RecordEntry.from_elements(*record_args)
                data = self._zipfile.read(item)
                if record.hash_ is None or record.size is None:
                    # Report empty hash / size
                    issues.append(
                        f"In {self._zipfile.filename}, hash / size of {item.filename} is not included in RECORD"
                    )
                elif not record.validate(data):
                    issues.append(
                        f"In {self._zipfile.filename}, hash / size of {item.filename} didn't match RECORD"
                    )

        if issues:
            raise _WheelFileValidationError(issues)

    def get_contents(self) -> Iterator[WheelContentElement]:
        """Sequential access to all contents of the wheel (including dist-info files).

        This implementation requires that every file that is a part of the wheel
        archive has a corresponding entry in RECORD. If they are not, an
        :any:`AssertionError` will be raised.
        """
        # Convert the record file into a useful mapping
        record_lines = self.read_dist_info("RECORD").splitlines()
        records = parse_record_file(record_lines)
        record_mapping = {record[0]: record for record in records}

        for item in self._zipfile.infolist():
            if item.filename[-1:] == "/":  # looks like a directory
                continue

            # Pop record with empty default, because validation is handled by `validate_record`
            record = record_mapping.pop(item.filename, (item.filename, "", ""))

            # Borrowed from:
            # https://github.com/pypa/pip/blob/0f21fb92/src/pip/_internal/utils/unpacking.py#L96-L100
            mode = item.external_attr >> 16
            is_executable = bool(mode and stat.S_ISREG(mode) and mode & 0o111)

            with self._zipfile.open(item) as stream:
                stream_casted = cast("BinaryIO", stream)
                yield record, stream_casted, is_executable
