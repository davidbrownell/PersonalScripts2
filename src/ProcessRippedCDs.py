# noqa: D100
import os
import re
import shutil

from enum import auto, Enum
from functools import cached_property
from pathlib import Path
from typing import Annotated, cast, Protocol, Self

import typer

from attr import dataclass
from dbrownell_Common.InflectEx import inflect  # type: ignore[import-untyped]
from dbrownell_Common.Streams.DoneManager import DoneManager, Flags as DoneManagerFlags  # type: ignore[import-untyped]
from dbrownell_Common import SubprocessEx  # type: ignore[import-untyped]
from typer.core import TyperGroup

# spell-checker: words flac, archiver, ALBUMARTIST, TOTALTRACKS, TRACKNUMBER


# ----------------------------------------------------------------------
class NaturalOrderGrouper(TyperGroup):
    """Ensure commands are listed in the order defined."""

    # pylint: disable=missing-class-docstring
    # ----------------------------------------------------------------------
    def list_commands(self, *args, **kwargs) -> list[str]:  # noqa: ARG002, D102
        return list(self.commands.keys())


# ----------------------------------------------------------------------
app = typer.Typer(
    cls=NaturalOrderGrouper,
    help=__doc__,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=False,
)


# ----------------------------------------------------------------------
@app.command("EntryPoint", no_args_is_help=True)
def EntryPoint(
    input_directory: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Input directory containing ripped .wav files, organized in subdirectories.",
        ),
    ],
    archive_output_dir: Annotated[
        Path,
        typer.Argument(
            exists=False,
            file_okay=False,
            resolve_path=True,
            help="Output directory populated with .7z files.",
        ),
    ],
    flac_output_dir: Annotated[
        Path,
        typer.Argument(
            exists=False,
            file_okay=False,
            resolve_path=True,
            help="Output directory populated with .flac files, organized in subdirectories.",
        ),
    ],
    verbose: Annotated[  # noqa: FBT002
        bool,
        typer.Option("--verbose", help="Write verbose information to the terminal."),
    ] = False,
    debug: Annotated[  # noqa: FBT002
        bool,
        typer.Option("--debug", help="Write debug information to the terminal."),
    ] = False,
) -> None:
    """Convert .wav files produced when ripping CDs for my personal backup."""

    with DoneManager.CreateCommandLine(
        flags=DoneManagerFlags.Create(verbose=verbose, debug=debug),
    ) as dm:
        directories: list[Path] = [directory for directory in input_directory.iterdir() if directory.is_dir()]

        if not directories:
            input_directory_is_source = True
            directories.append(input_directory)
        else:
            input_directory_is_source = False

        albums: list[_Album] = []

        with dm.Nested(
            "Processing {}...".format(inflect.no("directory", len(directories))),
            lambda: "{} found".format(inflect.no("album", len(albums))),
            suffix="\n",
        ) as albums_dm:
            for index, directory in enumerate(directories):
                album: _Album | None = None

                with albums_dm.Nested(
                    "Processing '{}' ({} of {})...".format(directory, index + 1, len(directories)),
                    lambda: "" if not album else "{} found".format(inflect.no("track", len(album.tracks))),  # noqa: B023
                ) as album_dm:
                    album = _Album.from_directory(album_dm, directory)
                    if album:
                        albums.append(album)

            if not albums:
                return

            # Warnings associated with invalid directories should not cause the entire process to fail
            if album_dm.result > 0:
                albums_dm.result = 0

        # Ensure that the archiver and encoder are available
        encoder = _GetEncoder(dm)
        if encoder is None:
            return

        archiver = _GetArchiver(dm)
        if archiver is None:
            return

        dm.WriteLine("")

        encode_errors = _EncodeContent(
            dm,
            flac_output_dir,
            albums,
            encoder,
            input_directory_is_source=input_directory_is_source,
        )

        dm.WriteLine("")

        _ArchiveContent(
            dm,
            archive_output_dir,
            albums,
            archiver,
            encode_errors,
            input_directory_is_source=input_directory_is_source,
        )


# ----------------------------------------------------------------------
# |
# |  Private Types
# |
# ----------------------------------------------------------------------
@dataclass
class _TrackMetadata:
    title: str
    artist: str
    track_num: int
    track_length: str
    composer: str
    album_title: str
    album_artist: str
    album_composer: str
    album_interpret: str
    year: int
    genre: str
    comment: str
    num_tracks: int
    track_offset: int
    cd_db_type: str
    cd_db_id: str

    # ----------------------------------------------------------------------
    @classmethod
    def from_tab_delimited_line(
        cls,
        line: str,
    ) -> Self:
        line = line.rstrip("\n").rstrip("\t")
        items = line.split("\t")
        assert len(items) == 16, (len(items), line)  # noqa: PLR2004

        return cls(
            title=items[0],
            artist=items[1],
            track_num=int(items[2]),
            track_length=items[3],
            composer=items[4],
            album_title=items[5],
            album_artist=items[6],
            album_composer=items[7],
            album_interpret=items[8],
            year=int(items[9]),
            genre=items[10],
            comment=items[11],
            num_tracks=int(items[12]),
            track_offset=int(items[13]),
            cd_db_type=items[14],
            cd_db_id=items[15],
        )


# ----------------------------------------------------------------------
@dataclass
class _Album:
    source_dir: Path
    tracks: list[_TrackMetadata]
    album_pic: Path | None
    metadata_filename: Path | None
    log_filename: Path | None
    wav_lookup: dict[int, Path]

    # ----------------------------------------------------------------------
    @classmethod
    def from_directory(  # noqa: C901
        cls,
        dm: DoneManager,
        directory: Path,
    ) -> Self | None:
        # Parse the directory
        wav_filenames: list[Path] = []
        metadata_filename: Path | None = None
        album_pic: Path | None = None
        log_filename: Path | None = None

        for item in directory.iterdir():
            if not item.is_file():
                dm.WriteWarning(f"The subdirectory '{item.name}' was not expected.\n")
                return None

            if item.suffix == ".wav":
                wav_filenames.append(item)
            elif item.suffix == ".txt":
                if metadata_filename is not None:
                    dm.WriteWarning("Multiple metadata files found.\n")
                    return None

                metadata_filename = item
            elif item.suffix == ".log":
                if log_filename is not None:
                    dm.WriteWarning("Multiple log files found.\n")
                    return None

                log_filename = item
            elif item.suffix in (".jpg", ".jpeg", ".png"):
                if album_pic is not None:
                    dm.WriteWarning("Multiple album pictures found.\n")
                    return None

                album_pic = item
            else:
                dm.WriteWarning(f"The filename '{item.name}' was not expected.\n")
                return None

        if not wav_filenames:
            dm.WriteWarning("No wav files were found.\n")
            return None

        if not metadata_filename:
            dm.WriteWarning("A metadata file was not found.\n")
            return None

        # Organize the wav files
        wav_lookup: dict[int, Path] = {}
        wav_regex = re.compile(r"(?P<track_num>\d+).+\.wav")

        for wav_filename in wav_filenames:
            match = wav_regex.match(wav_filename.name)
            if not match:
                dm.WriteWarning(f"The wav filename '{wav_filename.name}' is not in the expected format.\n")
                return None

            track_num = int(match.group("track_num"))

            prev_track = wav_lookup.get(track_num)
            if prev_track is not None:
                dm.WriteWarning(
                    f"Multiple wav files were found for track '{track_num}': '{prev_track.name}' and '{wav_filename.name}'.\n",
                )
                return None

            wav_lookup[track_num] = wav_filename

        # Extract the metadata
        with metadata_filename.open(encoding="utf-16le") as f:
            lines = f.readlines()

        tracks: list[_TrackMetadata] = []

        for index, line in enumerate(lines):
            try:
                track_metadata = _TrackMetadata.from_tab_delimited_line(line)
            except Exception as ex:
                dm.WriteWarning(f"Invalid metadata was encountered: '{ex}' (line: {index + 1}).\n")
                return None

            tracks.append(track_metadata)

        return cls(
            source_dir=directory,
            tracks=tracks,
            album_pic=album_pic,
            metadata_filename=metadata_filename,
            log_filename=log_filename,
            wav_lookup=wav_lookup,
        )

    # ----------------------------------------------------------------------
    @cached_property
    def name(self) -> str:
        return f"{self.tracks[0].album_artist} - {self.tracks[0].year} - {self.tracks[0].album_title}"


# ----------------------------------------------------------------------
class _InvokeResult(Enum):
    """Result of invoking a command."""

    Skipped = auto()
    Success = auto()
    Failure = auto()


# ----------------------------------------------------------------------
# |
# |  Private Functions
# |
# ----------------------------------------------------------------------
class _Archiver(Protocol):
    """Functor that archives a directory."""

    def __call__(
        self,
        dm: DoneManager,
        album: _Album,
        output_dir: Path,
        output_name: str,
    ) -> _InvokeResult: ...


def _GetArchiver(dm: DoneManager) -> _Archiver | None:
    with dm.Nested("Checking for '7zip'...") as archive_dm:
        if os.name == "nt":
            binary_name = "7z"
        else:
            msg = f"'{os.name}' is not supported"
            raise NotImplementedError(msg)

        result = SubprocessEx.Run(binary_name)
        if result.returncode != 0:
            archive_dm.WriteError(result.output)
            return None

        # ----------------------------------------------------------------------
        def Archive(
            dm: DoneManager,
            album: _Album,
            output_dir: Path,
            output_name: str,
        ) -> _InvokeResult:
            output_filename = output_dir / f"{output_name}.7z"
            if output_filename.is_file():
                return _InvokeResult.Skipped

            temp_filename = output_filename.with_suffix(".7z_temp")
            temp_filename.unlink(missing_ok=True)

            # Archive
            with dm.Nested(
                "Archiving...",
                suffix="\n",
            ) as archive_dm:
                command_line = f'{binary_name} a -t7z -mx9 -sccUTF-8 -scsUTF-8 -ssw "{temp_filename}"'

                archive_dm.WriteVerbose(f"Command Line: {command_line}\n\n")

                with archive_dm.YieldStream() as stream:
                    archive_dm.result = SubprocessEx.Stream(
                        command_line,
                        stream,
                        cwd=album.source_dir,
                    )

                    if archive_dm.result != 0:
                        return _InvokeResult.Failure

            # Validate
            with dm.Nested("Validating...", suffix="\n") as validate_dm:
                command_line = f'{binary_name} t "{temp_filename}"'

                validate_dm.WriteVerbose(f"Command Line: {command_line}\n\n")

                with validate_dm.YieldStream() as stream:
                    validate_dm.result = SubprocessEx.Stream(command_line, stream)
                    if validate_dm.result != 0:
                        return _InvokeResult.Failure

            # Commit
            with dm.Nested("Committing..."):
                temp_filename.rename(output_filename)

            return _InvokeResult.Success

        # ----------------------------------------------------------------------

        return Archive


# ----------------------------------------------------------------------
class _Encoder(Protocol):
    """Functor that encodes a wav file."""

    def __call__(
        self,
        dm: DoneManager,
        album: _Album,
        output_dir: Path,
    ) -> _InvokeResult: ...


def _GetEncoder(dm: DoneManager) -> _Encoder | None:
    with dm.Nested("Checking for 'flac'...") as encode_dm:
        binary_name = "flac"

        result = SubprocessEx.Run(f"{binary_name} --version")
        if result.returncode != 0:
            encode_dm.WriteError(result.output)
            return None

        # ----------------------------------------------------------------------
        def Encode(
            dm: DoneManager,
            album: _Album,
            output_dir: Path,
        ) -> _InvokeResult:
            if output_dir.is_dir():
                return _InvokeResult.Skipped

            temp_dir = output_dir.with_suffix(".tmp")
            if temp_dir.is_dir():
                shutil.rmtree(temp_dir)
            temp_dir.mkdir(parents=True)

            # Encoding
            with dm.Nested(
                "Encoding tracks...",
                suffix="\n",
            ) as encoding_dm:
                for track_index, track in enumerate(album.tracks):
                    with encoding_dm.Nested(
                        "Processing '{}' ({} of {})...".format(
                            track.title, track_index + 1, len(album.tracks)
                        ),
                        suffix="\n",
                    ) as track_dm:
                        wav_filename = album.wav_lookup.pop(track.track_num, None)
                        if wav_filename is None:
                            if track.title in ["Data", "Data Track"]:
                                # Some CDs include a data files, which is a txt file that won't be copied
                                # when ripping the CD. This is not an error, but rather a file that should
                                # be skipped.
                                continue

                            track_dm.WriteError(
                                f"The track number '{track.track_num}' was not found or has already been used.\n"
                            )
                            return _InvokeResult.Failure

                        temp_filename = temp_dir / f"temp_{track.track_num}"
                        temp_filename.unlink(missing_ok=True)

                        command_line_parts: list[str] = [
                            binary_name,
                            "-8",
                            "--verify",
                            f'-T "ARTIST={track.artist.replace('"', r"\"")}"',
                            f'-T "TITLE={track.title.replace('"', r"\"")}"',
                            f'-T "ALBUM={track.album_title.replace('"', r"\"")}"',
                            f'-T "DATE={track.year}"',
                            f'-T "TRACKNUMBER={track.track_num}"',
                            f'-T "GENRE={track.genre.replace('"', r"\"")}"',
                            f'-T "COMMENT={track.comment.replace('"', r"\"")}"',
                            f'-T "BAND={track.album_interpret.replace('"', r"\"")}"',
                            f'-T "ALBUMARTIST={track.album_interpret.replace('"', r"\"")}"',
                            f'-T "COMPOSER={track.composer.replace('"', r"\"")}"',
                            f'-T "TOTALTRACKS={track.num_tracks}"',
                            f'"{str(wav_filename).replace('"', r"\"")}"',
                            f'--output-name "{str(temp_filename).replace('"', r"\"")}"',
                        ]

                        if album.album_pic:
                            command_line_parts.append(f'"--picture={album.album_pic}"')

                        command_line = " ".join(command_line_parts)

                        track_dm.WriteVerbose(f"Command Line: {command_line}\n\n")

                        with track_dm.YieldStream() as stream:
                            track_dm.result = SubprocessEx.Stream(command_line, stream)
                            if track_dm.result != 0:
                                return _InvokeResult.Failure

                        # Rename the file
                        temp_filename.rename(temp_dir / wav_filename.with_suffix(".flac").name)

                if album.wav_lookup:
                    encoding_dm.WriteError(
                        "The following wav files were not processed: {}\n".format(
                            ", ".join(f"'{filename.name}'" for filename in album.wav_lookup.values()),
                        ),
                    )
                    return _InvokeResult.Failure

            # Copy the album art
            if album.album_pic:
                with dm.Nested("Copying album art..."):
                    temp_filename = temp_dir / "album_art.temp"
                    temp_filename.unlink(missing_ok=True)

                    shutil.copy(album.album_pic, temp_filename)
                    temp_filename.rename(temp_dir / album.album_pic.name)

            # Commit
            with dm.Nested("Committing..."):
                temp_dir.rename(output_dir)

            return _InvokeResult.Success

        # ----------------------------------------------------------------------

        return Encode


# ----------------------------------------------------------------------
def _EncodeContent(
    dm: DoneManager,
    flac_output_dir: Path,
    albums: list[_Album],
    encoder: _Encoder,
    *,
    input_directory_is_source: bool,
) -> set[int]:  # Returns the ids of album instances that were not successfully encoded
    encode_errors: set[int] = set()
    num_encoded = 0

    with dm.Nested(
        "Encoding content...",
        lambda: "{} encoded".format(inflect.no("album", num_encoded)),
    ) as encode_dm:
        # ----------------------------------------------------------------------
        def GetInvokeResultSuffix(
            result: _InvokeResult,
        ) -> str:
            if result == _InvokeResult.Skipped:
                return "Already encoded"
            if result == _InvokeResult.Success:
                return "Encoded"
            if result == _InvokeResult.Failure:
                return "Encoding failed"

            assert False, result  # noqa: B011, PT015

        # ----------------------------------------------------------------------

        for album_index, album in enumerate(albums):
            encode_result: _InvokeResult | None = None

            with encode_dm.Nested(
                "Processing '{}' ({} of {})...".format(album.name, album_index + 1, len(albums)),
                lambda: GetInvokeResultSuffix(cast("_InvokeResult", encode_result)),  # noqa: B023
                suffix=lambda: "\n" if encode_result == _InvokeResult.Success else None,  # noqa: B023
            ) as album_encode_dm:
                if input_directory_is_source:
                    encode_output_dir = flac_output_dir
                else:
                    encode_output_dir = flac_output_dir / album.source_dir.name

                encode_result = encoder(album_encode_dm, album, encode_output_dir)

                if encode_result == _InvokeResult.Success:
                    num_encoded += 1
                elif encode_result == _InvokeResult.Failure:
                    encode_errors.add(id(album))

    return encode_errors


# ----------------------------------------------------------------------
def _ArchiveContent(
    dm: DoneManager,
    archive_output_dir: Path,
    albums: list[_Album],
    archiver: _Archiver,
    encode_errors: set[int],
    *,
    input_directory_is_source: bool,
) -> None:
    num_archived = 0

    with dm.Nested(
        "Archiving content...",
        lambda: "{} archived".format(inflect.no("album", num_archived)),
        suffix="\n",
    ) as archive_dm:
        # ----------------------------------------------------------------------
        def GetInvokeResultSuffix(
            result: _InvokeResult,
        ) -> str:
            if result == _InvokeResult.Skipped:
                return "Already archived"
            if result == _InvokeResult.Success:
                return "Archived"
            if result == _InvokeResult.Failure:
                return "Archiving failed"

            assert False, result  # noqa: B011, PT015

        # ----------------------------------------------------------------------

        for album_index, album in enumerate(albums):
            if id(album) in encode_errors:
                archive_dm.WriteWarning(f"Skipping '{album.name}' due to encoding errors.\n")
                continue

            archive_result: _InvokeResult | None = None

            with archive_dm.Nested(
                "Processing '{}' ({} of {})...".format(album.name, album_index, len(albums)),
                lambda: GetInvokeResultSuffix(cast("_InvokeResult", archive_result)),  # noqa: B023
                suffix=lambda: "\n" if archive_result == _InvokeResult.Success else None,  # noqa: B023
            ) as album_archive_dm:
                archive_result = archiver(
                    album_archive_dm,
                    album,
                    archive_output_dir,
                    "archive" if input_directory_is_source else album.source_dir.name,
                )

                if archive_result == _InvokeResult.Success:
                    num_archived += 1


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
if __name__ == "__main__":
    app()
