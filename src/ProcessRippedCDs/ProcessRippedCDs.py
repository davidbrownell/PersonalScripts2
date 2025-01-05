import os
import re
import shutil

from functools import cached_property
from pathlib import Path
from typing import Annotated, Protocol, Self

import typer

from attr import dataclass
from dbrownell_Common.InflectEx import inflect  # type: ignore[import-untyped]
from dbrownell_Common.Streams.DoneManager import DoneManager, Flags as DoneManagerFlags  # type: ignore[import-untyped]
from dbrownell_Common import SubprocessEx  # type: ignore[import-untyped]
from typer.core import TyperGroup


# ----------------------------------------------------------------------
class NaturalOrderGrouper(TyperGroup):
    # pylint: disable=missing-class-docstring
    # ----------------------------------------------------------------------
    def list_commands(self, *args, **kwargs):  # pylint: disable=unused-argument
        return self.commands.keys()


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
        typer.Argument(exists=True, file_okay=False, resolve_path=True),
    ],
    archive_output_dir: Annotated[
        Path,
        typer.Argument(exists=False, file_okay=False, resolve_path=True),
    ],
    flac_output_dir: Annotated[
        Path,
        typer.Argument(exists=False, file_okay=False, resolve_path=True),
    ],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Write verbose information to the terminal."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", help="Write debug information to the terminal."),
    ] = False,
) -> None:
    """Process wav files produced when ripping CDs for my personal backup."""

    with DoneManager.CreateCommandLine(
        flags=DoneManagerFlags.Create(verbose=verbose, debug=debug),
    ) as dm:
        directories: list[Path] = []

        for directory in input_directory.iterdir():
            if directory.is_dir():
                directories.append(directory)

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
                    lambda: "" if not album else "{} found".format(inflect.no("track", len(album.tracks))),
                ) as album_dm:
                    album = _Album.from_directory(album_dm, directory)
                    if album:
                        albums.append(album)

            if not albums:
                return

            # Warnings associated with invalid directories should not cause the entire process to fail
            albums_dm.result = 0

        # Ensure that the archiver and encoder are available
        archiver = _GetArchiver(dm)
        if archiver is None:
            return

        encoder = _GetEncoder(dm)
        if encoder is None:
            return

        dm.WriteLine("")

        # Archive the content
        with dm.Nested("Archiving content...", suffix="\n") as archive_dm:
            for index, album in enumerate(albums):
                if input_directory_is_source:
                    archive_name = "archive"
                else:
                    archive_name = album.source_dir.name

                archiver(archive_dm, album, index, len(albums), archive_output_dir, archive_name)

        # Encode the content
        with dm.Nested("Encoding content...") as encode_dm:
            for index, album in enumerate(albums):
                if input_directory_is_source:
                    encode_output_dir = flac_output_dir
                else:
                    encode_output_dir = flac_output_dir / album.source_dir.name

                encoder(encode_dm, album, index, len(albums), encode_output_dir)


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
        assert len(items) == 16, (len(items), line)

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
    def from_directory(
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
            dm.WriteWarning("Metadata files were not found.\n")
            return None

        # Organize the wav files
        wav_lookup: dict[int, Path] = {}
        wav_regex = re.compile(r"(?P<track_num>\d+).*.wav")

        for wav_filename in wav_filenames:
            match = wav_regex.match(wav_filename.name)
            if not match:
                dm.WriteWarning(f"The wav filename '{wav_filename.name}' is not in the expected format.\n")
                return None

            track_num = int(match.group("track_num"))

            prev_track = wav_lookup.get(track_num, None)
            if prev_track is not None:
                dm.WriteWarning(
                    f"Multiple wav files were found for track '{track_num}': '{prev_track.name}' and '{wav_filename.name}'.\n"
                )
                return None

            wav_lookup[track_num] = wav_filename

        # Extract the metadata
        with metadata_filename.open(encoding="utf-16le") as f:
            lines = f.readlines()

        tracks: list[_TrackMetadata] = []

        for index, line in enumerate(lines):
            try:
                tracks.append(_TrackMetadata.from_tab_delimited_line(line))
            except Exception as ex:
                dm.WriteWarning(f"Invalid metadata was encountered: '{str(ex)}' (line: {index + 1}).\n")
                return None

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
# |
# |  Private Functions
# |
# ----------------------------------------------------------------------
class _Archiver(Protocol):
    """Functor that archives a directory"""

    def __call__(
        self,
        dm: DoneManager,
        album: _Album,
        index: int,
        num_albums: int,
        output_dir: Path,
        output_name: str,
    ) -> None: ...


def _GetArchiver(dm: DoneManager) -> _Archiver | None:
    with dm.Nested("Checking for '7zip'...") as archive_dm:
        if os.name == "nt":
            binary_name = "7z"
        else:
            raise NotImplementedError(f"'{os.name}' is not supported")

        result = SubprocessEx.Run(binary_name)
        if result.returncode != 0:
            archive_dm.WriteError(result.output)
            return None

        # ----------------------------------------------------------------------
        def Archive(
            dm: DoneManager,
            album: _Album,
            index: int,
            num_albums: int,
            output_dir: Path,
            output_name: str,
        ) -> None:
            already_archived = False

            with dm.Nested(
                "Processing '{}' ({} of {})...".format(album.name, index + 1, num_albums),
                lambda: "Already archived" if already_archived else "Archived",
                suffix=lambda: None if already_archived else "\n",
            ) as album_dm:
                output_filename = output_dir / f"{output_name}.7z"
                if output_filename.is_file():
                    already_archived = True
                    return

                temp_filename = output_filename.with_suffix(".7z_temp")
                temp_filename.unlink(missing_ok=True)

                # Archive
                with album_dm.Nested(
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
                            return

                # Validate
                with album_dm.Nested("Validating...", suffix="\n") as validate_dm:
                    command_line = f'{binary_name} t "{temp_filename}"'

                    validate_dm.WriteVerbose(f"Command Line: {command_line}\n\n")

                    with validate_dm.YieldStream() as stream:
                        validate_dm.result = SubprocessEx.Stream(command_line, stream)
                        if validate_dm.result != 0:
                            return

                # Commit
                with album_dm.Nested("Committing..."):
                    temp_filename.rename(output_filename)

        # ----------------------------------------------------------------------

        return Archive


# ----------------------------------------------------------------------
class _Encoder(Protocol):
    """Functor that encodes a wav file"""

    def __call__(
        self,
        dm: DoneManager,
        album: _Album,
        index: int,
        num_albums: int,
        output_dir: Path,
    ) -> None: ...


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
            index: int,
            num_albums: int,
            output_dir: Path,
        ) -> None:
            already_encoded = False

            with dm.Nested(
                "Processing '{}' ({} of {})...".format(album.name, index + 1, num_albums),
                lambda: "Already encoded" if already_encoded else "Encoded",
                suffix=lambda: None if already_encoded else "\n",
            ) as album_dm:
                if output_dir.is_dir():
                    already_encoded = True
                    return

                temp_dir = output_dir.with_suffix(".tmp")
                if temp_dir.is_dir():
                    shutil.rmtree(temp_dir)
                temp_dir.mkdir(parents=True)

                # Encoding
                with album_dm.Nested(
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
                                track_dm.WriteError(
                                    f"The track number '{track.track_num}' was not found or has already been used.\n"
                                )
                                return

                            temp_filename = temp_dir / f"temp_{track.track_num}"
                            temp_filename.unlink(missing_ok=True)

                            command_line_parts: list[str] = [
                                binary_name,
                                "-8",
                                "--verify",
                                f'-T "ARTIST={track.artist}"',
                                f'-T "TITLE={track.title}"',
                                f'-T "ALBUM={track.album_title}"',
                                f'-T "DATE={track.year}"',
                                f'-T "TRACKNUMBER={track.track_num}"',
                                f'-T "GENRE={track.genre}"',
                                f'-T "COMMENT={track.comment}"',
                                f'-T "BAND={track.album_interpret}"',
                                f'-T "ALBUMARTIST={track.album_interpret}"',
                                f'-T "COMPOSER={track.composer}"',
                                f'-T "TOTALTRACKS={track.num_tracks}"',
                                f'"{wav_filename}"',
                                f'--output-name "{temp_filename}"',
                            ]

                            if album.album_pic:
                                command_line_parts.append(f'"--picture={album.album_pic}"')

                            command_line = " ".join(command_line_parts)

                            track_dm.WriteVerbose(f"Command Line: {command_line}\n\n")

                            with track_dm.YieldStream() as stream:
                                track_dm.result = SubprocessEx.Stream(command_line, stream)
                                if track_dm.result != 0:
                                    return

                            # Rename the file
                            temp_filename.rename(temp_dir / wav_filename.with_suffix(".flac").name)

                    if album.wav_lookup:
                        encoding_dm.WriteError(
                            "The following wav files were not processed: {}\n".format(
                                ", ".join(f"'{filename.name}'" for filename in album.wav_lookup.values()),
                            ),
                        )
                        return

                # Copy the album art
                if album.album_pic:
                    with album_dm.Nested("Copying album art..."):
                        temp_filename = temp_dir / "album_art.temp"
                        temp_filename.unlink(missing_ok=True)

                        shutil.copy(album.album_pic, temp_filename)
                        temp_filename.rename(temp_dir / album.album_pic.name)

                # Commit
                with album_dm.Nested("Committing..."):
                    temp_dir.rename(output_dir)

        # ----------------------------------------------------------------------

        return Encode


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
if __name__ == "__main__":
    app()
