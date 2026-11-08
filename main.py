# Federal Register "6039G" notice downloader (Quarterly Publication of Individuals, Who Have Chosen To Expatriate).
# Downloads the .txt version of every notice into TXTs/ and the original .pdf version into PDFs/.
# There are no command line arguments: just run "python main.py". All settings are constants in section 2.
# Requires Python 3.12 or newer, because it uses the "type" statement (a soft keyword).
# The whole application lives in this one file. Every line of code carries a comment.
#
# File layout:
#   1. Imports
#   2. Constants and shared state
#   3. Types (alias, exception, enums, dataclasses)
#   4. Logging setup (everything goes to the console)
#   5. HTTP layer
#   6. Search layer (Federal Register API)
#   7. Download layer
#   8. Main

# ======================================================================================
# 1. Imports
# ======================================================================================

import asyncio  # runs the downloads concurrently
import collections  # provides Counter for the final summary
import contextlib  # provides suppress for cleaning up temporary files
import dataclasses  # provides the dataclass decorator for the records
import datetime  # validates the publication dates
import enum  # provides Enum for the file kinds and download outcomes
import functools  # provides partial for pre-filling function arguments
import hashlib  # computes a checksum of every downloaded file
import http  # provides HTTPStatus for readable status codes
import itertools  # provides count and chain.from_iterable
import json  # decodes the API responses
import logging  # prints every message to the console
import math  # provides ceil for the page count calculation
import os  # provides replace for atomic file moves and environ for the contact email
import pathlib  # handles file system paths
import re  # provides regular expressions for the file name filter
import socket  # provides the timeout exception type
import sys  # provides version information and the console stream
import tempfile  # creates temporary files next to the final files
import threading  # provides Lock for the shared request counter
import time  # provides sleep and the stopwatch
import types  # provides TracebackType for the exception hook annotation
import typing  # provides type hint helpers
import urllib.error  # provides HTTPError and URLError
import urllib.parse  # provides urlencode for query strings
import urllib.request  # provides build_opener and Request

# ======================================================================================
# 2. Constants and shared state
# ======================================================================================

BASE_DIRECTORY = (
    pathlib.Path(__file__).resolve().parent
)  # the folder that contains this script, so the program works from any current directory
TEXT_DIRECTORY = BASE_DIRECTORY / "TXTs"  # where the .txt files are saved
PDF_DIRECTORY = BASE_DIRECTORY / "PDFs"  # where the .pdf files are saved
CONTACT_EMAIL = (
    os.environ.get("CONTACT_EMAIL") or "you@example.com"
)  # sent in the User-Agent header, set the CONTACT_EMAIL environment variable to use your own
API_DOCUMENTS_URL = "https://www.federalregister.gov/api/v1/documents.json"  # the search endpoint of the Federal Register API
SEARCH_TERMS = ("6039G",)  # the only search term used
REQUESTED_FIELD_NAMES = (
    "document_number",
    "title",
    "publication_date",
    "raw_text_url",
    "pdf_url",
    "html_url",
)  # only the fields we need
MAXIMUM_RESULTS_PER_PAGE = 1000  # the documented maximum page size
API_RESULT_CEILING = 10000  # the API stops counting here, so a count this high means "narrow the search"
MAXIMUM_CONCURRENT_DOWNLOADS = (
    4  # how many files are downloaded at the same time
)
DELAY_SECONDS = (
    1.0  # pause after each real download, to be polite to the server
)
MAXIMUM_ATTEMPTS = 4  # how many times one URL is tried before giving up
TIMEOUT_SECONDS = 60.0  # how long one request may take
MAXIMUM_RETRY_AFTER_SECONDS = 120.0  # the longest pause we accept when a server sends a Retry-After header
PDF_MAGIC_BYTES = b"%PDF"  # every valid PDF file starts with these four bytes
EXIT_SUCCESS = 0  # everything worked
EXIT_PROBLEM = 1  # nothing was found, or at least one file failed
EXIT_SEARCH_FAILED = 2  # the Federal Register search itself failed
CONSOLE_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(funcName)s: %(message)s"  # timestamp, level, thread, function, message
HTTP_OPENER = urllib.request.build_opener(
    urllib.request.HTTPHandler(debuglevel=1),
    urllib.request.HTTPSHandler(debuglevel=1),
)  # prints raw HTTP headers to the console

total_http_request_count = 0  # module level counter of every HTTP request made
request_count_lock = (
    threading.Lock()
)  # protects the counter because downloads run in threads

# ======================================================================================
# 3. Types
# ======================================================================================

type DocumentNumber = str  # a type alias, for example "2026-22229"


class ExpatriateNoticeError(
    Exception
):  # our own exception type for expected failures
    pass  # no extra behaviour is needed


class FileKind(enum.Enum):  # the two versions of every notice that we download
    TEXT = "txt"  # the plain text version, the value doubles as the file extension
    PDF = "pdf"  # the original PDF version, the value doubles as the file extension


class DownloadOutcome(enum.Enum):  # the three possible results of one download
    DOWNLOADED = "downloaded"  # the file was fetched and saved
    SKIPPED_EXISTING = (
        "skipped (already downloaded)"  # the file was already on disk
    )
    FAILED = "failed"  # the file could not be fetched or saved


@dataclasses.dataclass(
    frozen=True
)  # an immutable record describing one Federal Register document
class FederalRegisterDocument:  # holds the fields we care about
    document_number: DocumentNumber  # for example "2026-22229"
    title: str  # the document title
    publication_date: str  # ISO date such as "2026-10-30"
    raw_text_url: str  # the URL of the .txt version
    pdf_url: str  # the URL of the .pdf version


@dataclasses.dataclass(
    frozen=True
)  # an immutable record describing one file to fetch
class DownloadTask:  # one document produces two of these, one per file kind
    document: FederalRegisterDocument  # the document this file belongs to
    file_kind: FileKind  # whether this is the TXT or the PDF
    url: str  # where to download it from
    destination_path: pathlib.Path  # where to save it


@dataclasses.dataclass  # a mutable record that collects the results of a whole run
class DownloadReport:  # returned by the download layer
    outcome_counter: collections.Counter[tuple[FileKind, DownloadOutcome]] = (
        dataclasses.field(default_factory=collections.Counter)
    )  # tally per file kind and outcome
    failed_tasks: list[DownloadTask] = dataclasses.field(
        default_factory=list
    )  # every file that failed, listed again at the end


# ======================================================================================
# 4. Logging setup (everything goes to the console)
# ======================================================================================


def log_uncaught_exception(
    exception_type: type[BaseException],
    exception_value: BaseException,
    exception_traceback: types.TracebackType | None,
) -> None:  # logs crashes before the program exits
    logging.critical(
        "Uncaught exception",
        exc_info=(exception_type, exception_value, exception_traceback),
    )  # writes the full traceback to the console


def configure_console_logging() -> (
    None
):  # sends every log message to the console at DEBUG level
    logging.basicConfig(
        level=logging.DEBUG,
        stream=sys.stdout,
        force=True,
        format=CONSOLE_LOG_FORMAT,
    )  # DEBUG level, console stream, replaces earlier handlers
    logging.captureWarnings(
        True
    )  # routes Python warnings into the log as well
    sys.excepthook = log_uncaught_exception  # routes uncaught exceptions into the log as well


# ======================================================================================
# 5. HTTP layer
# ======================================================================================


def build_request(
    url: str,
) -> urllib.request.Request:  # builds a polite HTTP request
    user_agent_text = f"expatriate-notice-downloader ({CONTACT_EMAIL})"  # identifies this client to the server
    return urllib.request.Request(
        url, headers={"User-Agent": user_agent_text}
    )  # attaches the identifying header


def fetch_bytes_with_retries(
    url: str,
) -> bytes:  # downloads a URL with back-off
    global total_http_request_count  # we will modify the module level counter
    for attempt_number in itertools.count(
        start=1
    ):  # counts attempts 1, 2, 3, ... until we return or raise
        with (
            request_count_lock
        ):  # only one thread may change the counter at a time
            total_http_request_count += 1  # records this request
            current_request_number = total_http_request_count  # copies the value while the lock is held
        failure_description = "unknown failure"  # reset before each attempt
        retry_delay_seconds = float(
            2**attempt_number
        )  # exponential back-off: 2, 4, 8 seconds, unless the server asks for something else
        logging.debug(
            f"GET {url} (attempt {attempt_number} of {MAXIMUM_ATTEMPTS}, request number {current_request_number})"
        )  # log the outgoing request
        started_at = time.monotonic()  # start the stopwatch for this request
        try:  # attempt the request
            with HTTP_OPENER.open(
                build_request(url), timeout=TIMEOUT_SECONDS
            ) as http_response:  # opens the connection
                response_bytes = http_response.read()  # reads the whole body
                logging.debug(
                    f"Response {http_response.status} {http_response.reason}, {len(response_bytes)} bytes, content type {http_response.headers.get('Content-Type')}, {time.monotonic() - started_at:.2f} seconds"
                )  # log the response details
        except (
            urllib.error.HTTPError
        ) as http_error:  # the server answered with an error status
            match http_error.code:  # decide what to do based on the status code
                case (
                    http.HTTPStatus.NOT_FOUND
                    | http.HTTPStatus.GONE
                    | http.HTTPStatus.FORBIDDEN
                    | http.HTTPStatus.BAD_REQUEST
                ):  # permanent problems
                    raise ExpatriateNoticeError(
                        f"HTTP {http_error.code} for {url}"
                    ) from http_error  # retrying cannot help
                case _:  # anything else, such as 429 or 5xx, may be temporary
                    failure_description = (
                        f"HTTP {http_error.code}"  # remember why we will retry
                    )
                    retry_after_text = (
                        http_error.headers.get("Retry-After", "")
                        if http_error.headers
                        else ""
                    )  # the server's own suggestion, in seconds
                    if (
                        retry_after_text.isdigit()
                    ):  # only whole seconds are supported
                        retry_delay_seconds = min(
                            float(retry_after_text),
                            MAXIMUM_RETRY_AFTER_SECONDS,
                        )  # obey the server, but never wait forever
        except (
            urllib.error.URLError,
            socket.timeout,
            ConnectionError,
        ) as network_error:  # network level trouble
            failure_description = f"network problem: {network_error}"  # remember why we will retry
        else:  # the request succeeded
            return response_bytes  # hand the body back to the caller
        finally:  # runs after every attempt
            logging.debug(
                f"Attempt {attempt_number} for {url} finished"
            )  # trace output for debugging
        if attempt_number >= MAXIMUM_ATTEMPTS:  # we are out of attempts
            raise ExpatriateNoticeError(
                f"Giving up on {url} after {attempt_number} attempts ({failure_description})"
            )  # report the failure
        logging.warning(
            f"{failure_description}; retrying {url} in {retry_delay_seconds:.0f} seconds (attempt {attempt_number} of {MAXIMUM_ATTEMPTS})"
        )  # tell the person
        time.sleep(retry_delay_seconds)  # wait before the next attempt


# ======================================================================================
# 6. Search layer (Federal Register API)
# ======================================================================================


def iterate_search_result_pages(
    search_term: str,
) -> typing.Iterator[
    dict[str, typing.Any]
]:  # yields the API pages for one term
    query_parameter_pairs = [  # starts the list of query parameters
        ("conditions[term]", search_term),  # the full text search term
        ("order", "oldest"),  # a stable order makes paging predictable
        (
            "per_page",
            str(MAXIMUM_RESULTS_PER_PAGE),
        ),  # ask for the largest page size
    ]  # ends the list
    query_parameter_pairs.extend(
        ("fields[]", field_name) for field_name in REQUESTED_FIELD_NAMES
    )  # ask for only the fields we need
    next_page_url: str | None = (
        f"{API_DOCUMENTS_URL}?{urllib.parse.urlencode(query_parameter_pairs)}"  # the first page URL
    )
    logging.debug(
        f"Searching for {search_term!r} with parameters {query_parameter_pairs}"
    )  # log the search request
    while (
        next_page_url is not None
    ):  # keep going while the API offers another page
        response_bytes = fetch_bytes_with_retries(
            next_page_url
        )  # download the page
        search_page = json.loads(
            response_bytes.decode("utf-8")
        )  # decode the JSON
        logging.debug(
            f"Page keys {sorted(search_page)}; count {search_page.get('count')}; total_pages {search_page.get('total_pages')}; rows {len(search_page.get('results', []))}; next_page_url {search_page.get('next_page_url')}"
        )  # log the page envelope
        yield search_page  # hand the page to the caller and pause here
        next_page_url = search_page.get(
            "next_page_url"
        )  # the API gives the next page URL or null
        if not search_page.get(
            "results"
        ):  # an empty page means we are finished
            break  # stop the loop


def build_fallback_raw_text_url(
    document_number: DocumentNumber, publication_date: str
) -> str:  # rebuilds the .txt URL from its known pattern
    date_path = publication_date.replace(
        "-", "/"
    )  # 2026-10-30 becomes 2026/10/30
    return f"https://www.federalregister.gov/documents/full_text/text/{date_path}/{document_number}.txt"  # same pattern as the example URL


def build_fallback_pdf_url(
    document_number: DocumentNumber, publication_date: str
) -> str:  # rebuilds the .pdf URL from its known pattern
    return f"https://www.govinfo.gov/content/pkg/FR-{publication_date}/pdf/{document_number}.pdf"  # same pattern as the govinfo example URL


def convert_row_to_document(
    row: dict[str, typing.Any],
) -> FederalRegisterDocument:  # turns an API row into our record
    document_number = str(
        row["document_number"]
    )  # the unique document identifier
    publication_date = datetime.date.fromisoformat(
        row["publication_date"]
    ).isoformat()  # validates the date format
    raw_text_url = row.get("raw_text_url") or build_fallback_raw_text_url(
        document_number, publication_date
    )  # prefer the API URL
    pdf_url = row.get("pdf_url") or build_fallback_pdf_url(
        document_number, publication_date
    )  # prefer the API URL
    logging.debug(
        f"Row {document_number}: date {publication_date}, title {row.get('title')!r}, txt url {raw_text_url} (fallback used: {not row.get('raw_text_url')}), pdf url {pdf_url} (fallback used: {not row.get('pdf_url')})"
    )  # log every row
    return FederalRegisterDocument(
        document_number,
        str(row.get("title") or ""),
        publication_date,
        raw_text_url,
        pdf_url,
    )  # builds the record


def collect_documents_for_search_term(
    search_term: str,
) -> list[FederalRegisterDocument]:  # gathers every result for one term
    collected_rows: list[dict[str, typing.Any]] = []  # raw rows from all pages
    reported_count = 0  # the total the API says exists
    maximum_page_count = 1  # how many pages we expect
    for page_index, search_page in enumerate(
        iterate_search_result_pages(search_term), start=1
    ):  # walk the pages
        if page_index == 1:  # only the first page tells us the total
            reported_count = int(
                search_page.get("count", 0)
            )  # read the reported total
            if (
                reported_count >= API_RESULT_CEILING
            ):  # the API cannot be trusted at this size
                raise ExpatriateNoticeError(
                    f"Search '{search_term}' hit the {API_RESULT_CEILING} result ceiling; narrow it by date"
                )  # stop loudly
            maximum_page_count = max(
                1, math.ceil(reported_count / MAXIMUM_RESULTS_PER_PAGE)
            )  # pages needed at the page size
        collected_rows.extend(
            search_page.get("results", [])
        )  # keep this page's rows
        if (
            page_index >= maximum_page_count
        ):  # never trust "until empty" paging
            break  # we have asked for every page we need
    if len(collected_rows) != reported_count:  # catches silent paging problems
        raise ExpatriateNoticeError(
            f"Collected {len(collected_rows)} rows but the API reported {reported_count}"
        )  # a clear error instead of an assert, which Python can switch off
    logging.info(
        f"Search {search_term!r} returned {reported_count} documents"
    )  # progress message
    return [
        convert_row_to_document(row) for row in collected_rows
    ]  # convert every row


def sort_documents_oldest_first(
    documents_by_number: dict[DocumentNumber, FederalRegisterDocument],
) -> list[
    FederalRegisterDocument
]:  # sorts every search hit, with no filtering
    sorted_documents = sorted(
        documents_by_number.values(),
        key=lambda document: (
            document.publication_date,
            document.document_number,
        ),
    )  # oldest first
    for (
        document
    ) in sorted_documents:  # log every document that will be checked
        logging.debug(
            f"Queued {document.document_number} ({document.publication_date}): {document.title}"
        )  # one line per document
    return sorted_documents  # hand back the sorted list


# ======================================================================================
# 7. Download layer
# ======================================================================================


def build_destination_file_name(
    document: FederalRegisterDocument, file_kind: FileKind
) -> str:  # decides the local file name
    raw_file_name = f"{document.publication_date}_{document.document_number}.{file_kind.value}"  # same base name for TXT and PDF, only the extension differs
    return re.sub(
        r"[^A-Za-z0-9._-]", "_", raw_file_name
    )  # replaces any unsafe character


def build_download_tasks(
    document: FederalRegisterDocument,
) -> list[DownloadTask]:  # creates the two tasks of one document
    return [  # one task for each file kind
        DownloadTask(
            document,
            FileKind.TEXT,
            document.raw_text_url,
            TEXT_DIRECTORY
            / build_destination_file_name(document, FileKind.TEXT),
        ),  # the TXT goes into the text folder
        DownloadTask(
            document,
            FileKind.PDF,
            document.pdf_url,
            PDF_DIRECTORY
            / build_destination_file_name(document, FileKind.PDF),
        ),  # the PDF goes into the PDFs folder
    ]  # ends the list


def remove_leftover_temporary_files() -> (
    None
):  # cleans up .part files left behind by an interrupted earlier run
    for directory in (TEXT_DIRECTORY, PDF_DIRECTORY):  # both output folders
        for leftover_path in directory.glob(
            "*.part"
        ):  # every unfinished download
            logging.warning(
                f"Removing leftover temporary file {leftover_path.name}"
            )  # tell the person
            with contextlib.suppress(
                OSError
            ):  # a file that cannot be removed is not worth stopping for
                leftover_path.unlink()  # delete it


def is_existing_file_usable(
    destination_path: pathlib.Path, file_kind: FileKind
) -> bool:  # decides whether a file on disk can be kept
    if (
        not destination_path.is_file() or destination_path.stat().st_size == 0
    ):  # missing or empty files must be downloaded again
        return False  # not usable
    if file_kind is FileKind.PDF:  # PDFs get an extra sanity check
        with destination_path.open(
            "rb"
        ) as existing_file:  # open the file for reading bytes
            return (
                existing_file.read(len(PDF_MAGIC_BYTES)) == PDF_MAGIC_BYTES
            )  # a broken download will not start with %PDF
    return True  # a non-empty TXT file is accepted


def validate_downloaded_bytes(
    task: DownloadTask, document_bytes: bytes
) -> None:  # rejects bad content before it is saved
    if not document_bytes:  # an empty body is a failure
        raise ExpatriateNoticeError(
            f"Empty response for {task.url}"
        )  # report it
    if task.file_kind is FileKind.PDF and not document_bytes.startswith(
        PDF_MAGIC_BYTES
    ):  # an HTML error page is not a PDF
        raise ExpatriateNoticeError(
            f"Response for {task.url} is not a PDF file"
        )  # report it


def download_one_file(
    task: DownloadTask,
) -> DownloadOutcome:  # downloads one file (blocking)
    destination_path = task.destination_path  # where the file will live
    if is_existing_file_usable(
        destination_path, task.file_kind
    ):  # already downloaded and valid
        logging.info(
            f"Skipping {destination_path.name} because it already exists"
        )  # tell the person
        return DownloadOutcome.SKIPPED_EXISTING  # nothing more to do
    temporary_path: pathlib.Path | None = (
        None  # remembers the temporary file for cleanup
    )
    try:  # attempt the download
        document_bytes = fetch_bytes_with_retries(
            task.url
        )  # fetch the content
        validate_downloaded_bytes(
            task, document_bytes
        )  # make sure the content looks right
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_path.parent,
            suffix=".part",
            delete=False,
        ) as temporary_file:  # write beside the target
            temporary_path = pathlib.Path(
                temporary_file.name
            )  # remember the temporary name
            temporary_file.write(document_bytes)  # save the content
        logging.debug(
            f"Wrote {len(document_bytes)} bytes to temporary file {temporary_path.name}"
        )  # log the temporary write
        os.replace(
            temporary_path, destination_path
        )  # atomically move it into place, so partial files never look complete
        logging.debug(
            f"Moved {temporary_path.name} to {destination_path}"
        )  # log the final move
    except (
        ExpatriateNoticeError,
        OSError,
    ) as download_error:  # expected failures
        logging.error(
            f"Failed {task.document.document_number} ({task.file_kind.value}): {download_error}"
        )  # report the failure
        return DownloadOutcome.FAILED  # tell the caller
    else:  # everything worked
        checksum_prefix = hashlib.sha256(document_bytes).hexdigest()[
            :12
        ]  # short checksum for the log
        logging.info(
            f"Downloaded {destination_path.name} ({len(document_bytes)} bytes, sha256 {checksum_prefix})"
        )  # report success
        return DownloadOutcome.DOWNLOADED  # tell the caller
    finally:  # always runs
        if temporary_path is not None:  # a temporary file was created
            with contextlib.suppress(
                FileNotFoundError
            ):  # it is already gone after a successful move
                temporary_path.unlink()  # remove any leftover partial file


async def download_all_files(
    tasks: list[DownloadTask],
) -> DownloadReport:  # downloads everything concurrently
    concurrency_limiter = asyncio.Semaphore(
        MAXIMUM_CONCURRENT_DOWNLOADS
    )  # caps simultaneous downloads
    download_report = DownloadReport()  # collects the tally and the failures
    completed_count = 0  # how many files are finished
    blocking_download = functools.partial(
        download_one_file
    )  # the function that runs inside a worker thread

    async def download_with_limit(
        task: DownloadTask,
    ) -> None:  # one asyncio task per file
        nonlocal completed_count  # we update the counter of the enclosing function
        async with concurrency_limiter:  # wait for a free slot
            outcome = await asyncio.to_thread(
                blocking_download, task
            )  # run the blocking download in a thread
            if (
                outcome is DownloadOutcome.DOWNLOADED
            ):  # only real downloads need a polite pause
                await asyncio.sleep(DELAY_SECONDS)  # be gentle with the server
        completed_count += 1  # one more file finished
        download_report.outcome_counter[
            (task.file_kind, outcome)
        ] += 1  # tally the outcome for this file kind
        if (
            outcome is DownloadOutcome.FAILED
        ):  # remember failures for the final report
            download_report.failed_tasks.append(
                task
            )  # keep the whole task so the URL can be shown
        logging.info(
            f"Progress: {completed_count} of {len(tasks)} files handled"
        )  # progress message

    await asyncio.gather(
        *(download_with_limit(task) for task in tasks)
    )  # run all tasks and wait for them, a failed PDF never stops a TXT
    return download_report  # hand back the report


# ======================================================================================
# 8. Main
# ======================================================================================


def main() -> int:  # the program entry point
    configure_console_logging()  # everything below is logged to the console
    started_at = time.monotonic()  # start the stopwatch for the whole run
    logging.debug(
        f"Python {sys.version.split()[0]} on {sys.platform}; contact email {CONTACT_EMAIL}; text folder {TEXT_DIRECTORY}; PDF folder {PDF_DIRECTORY}"
    )  # log the environment and the settings
    if CONTACT_EMAIL == "you@example.com":  # nobody has set a real address
        logging.warning(
            "CONTACT_EMAIL is not set; set the environment variable to identify yourself politely to the API"
        )  # gentle reminder
    TEXT_DIRECTORY.mkdir(
        parents=True, exist_ok=True
    )  # create the text folder when missing
    PDF_DIRECTORY.mkdir(
        parents=True, exist_ok=True
    )  # create the PDF folder when missing
    remove_leftover_temporary_files()  # clean up after any interrupted earlier run
    try:  # searching can fail
        documents_per_term = [
            collect_documents_for_search_term(search_term)
            for search_term in SEARCH_TERMS
        ]  # one list of documents per search term
    except (
        ExpatriateNoticeError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
    ) as search_error:  # an expected failure, including unexpected API content
        logging.error(f"Search failed: {search_error}")  # tell the person
        return EXIT_SEARCH_FAILED  # exit code for search failures
    documents_by_number = {
        document.document_number: document
        for document in itertools.chain.from_iterable(documents_per_term)
    }  # merges and removes duplicates
    logging.debug(
        f"{len(documents_by_number)} unique documents after merging all searches"
    )  # log the merge result
    del documents_per_term  # the per-term lists are no longer needed
    matching_documents = sort_documents_oldest_first(
        documents_by_number
    )  # sort every search hit
    logging.info(
        f"{len(matching_documents)} matching documents found"
    )  # progress message
    if not matching_documents:  # nothing to do
        logging.warning("No matching documents were found.")  # tell the person
        return EXIT_PROBLEM  # exit code for "nothing found"
    download_tasks = list(
        itertools.chain.from_iterable(
            build_download_tasks(document) for document in matching_documents
        )
    )  # two tasks per document, TXT and PDF side by side
    logging.info(
        f"{len(download_tasks)} files to check ({len(matching_documents)} TXT and {len(matching_documents)} PDF)"
    )  # progress message
    download_report = asyncio.run(
        download_all_files(download_tasks), debug=True
    )  # download everything with asyncio debug logging
    for file_kind in FileKind:  # one block of summary lines per file kind
        for outcome in DownloadOutcome:  # log every outcome, even when zero
            logging.info(
                f"Summary - {file_kind.value.upper()} {outcome.value}: {download_report.outcome_counter[(file_kind, outcome)]}"
            )  # one line per kind and outcome
    logging.info(
        f"Summary - HTTP requests made: {total_http_request_count}"
    )  # shows the global counter
    logging.info(
        f"Summary - total time: {time.monotonic() - started_at:.1f} seconds"
    )  # shows how long the run took
    for (
        failed_task
    ) in (
        download_report.failed_tasks
    ):  # list every failure once more, so it is easy to find
        logging.error(
            f"Still missing: {failed_task.destination_path.name} from {failed_task.url}"
        )  # one line per failed file
    if download_report.failed_tasks:  # at least one file failed
        logging.error(
            "Run the program again to retry the missing files; existing files are skipped."
        )  # tell the person what to do
        return EXIT_PROBLEM  # non-zero exit code when anything failed
    return EXIT_SUCCESS  # everything is on disk


if __name__ == "__main__":  # only when run as a script
    raise SystemExit(main())  # exit with the return value of main
