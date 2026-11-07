# Federal Register "6039G" notice downloader (Quarterly Publication of Individuals, Who Have Chosen To Expatriate).
# Requires Python 3.12 or newer, because it uses the "type" statement (a soft keyword).
# The whole application lives in this one file. Every line of code carries a comment.
#
# File layout:
#   1. Imports
#   2. Constants and shared state
#   3. Types (alias, exception, enum, dataclass)
#   4. Logging setup (everything goes to the console)
#   5. HTTP layer
#   6. Search layer (Federal Register API)
#   7. Download layer
#   8. Command line and main

# ======================================================================================
# 1. Imports
# ======================================================================================

import argparse  # parses the command line arguments
import asyncio  # runs the downloads concurrently
import collections  # provides Counter for the final summary
import contextlib  # provides suppress for cleaning up temporary files
import dataclasses  # provides the dataclass decorator for the document record
import datetime  # validates the publication dates
import enum  # provides Enum for the download outcomes
import functools  # provides partial for pre-filling function arguments
import hashlib  # computes a checksum of every downloaded file
import http  # provides HTTPStatus for readable status codes
import itertools  # provides count and chain.from_iterable
import json  # decodes the API responses
import logging  # prints every message to the console
import math  # provides ceil for the page count calculation
import os  # provides replace for atomic file moves
import pathlib  # handles file system paths
import re  # provides regular expressions for the title filter
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

API_DOCUMENTS_URL = "https://www.federalregister.gov/api/v1/documents.json"  # the search endpoint of the Federal Register API
DEFAULT_SEARCH_TERMS = ("6039G",)  # the only search term used by default
REQUESTED_FIELD_NAMES = ("document_number", "title", "publication_date", "raw_text_url", "html_url")  # only the fields we need
MAXIMUM_RESULTS_PER_PAGE = 1000  # the documented maximum page size
API_RESULT_CEILING = 10000  # the API stops counting here, so a count this high means "narrow the search"
CONSOLE_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(funcName)s: %(message)s"  # timestamp, level, thread, function, message
HTTP_OPENER = urllib.request.build_opener(urllib.request.HTTPHandler(debuglevel=1), urllib.request.HTTPSHandler(debuglevel=1))  # prints raw HTTP headers to the console

total_http_request_count = 0  # module level counter of every HTTP request made
request_count_lock = threading.Lock()  # protects the counter because downloads run in threads

# ======================================================================================
# 3. Types
# ======================================================================================

type DocumentNumber = str  # a type alias, for example "2026-22229"


class ExpatriateNoticeError(Exception):  # our own exception type for expected failures
    pass  # no extra behaviour is needed


class DownloadOutcome(enum.Enum):  # the three possible results of one download
    DOWNLOADED = "downloaded"  # the file was fetched and saved
    SKIPPED_EXISTING = "skipped (already downloaded)"  # the file was already on disk
    FAILED = "failed"  # the file could not be fetched or saved


@dataclasses.dataclass(frozen=True)  # an immutable record describing one Federal Register document
class FederalRegisterDocument:  # holds the fields we care about
    document_number: DocumentNumber  # for example "2026-22229"
    title: str  # the document title
    publication_date: str  # ISO date such as "2026-10-30"
    raw_text_url: str  # the URL of the .txt version

# ======================================================================================
# 4. Logging setup (everything goes to the console)
# ======================================================================================


def log_uncaught_exception(exception_type: type[BaseException], exception_value: BaseException, exception_traceback: types.TracebackType | None) -> None:  # logs crashes before the program exits
    logging.critical("Uncaught exception", exc_info=(exception_type, exception_value, exception_traceback))  # writes the full traceback to the console


def configure_console_logging() -> None:  # sends every log message to the console at DEBUG level
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, force=True, format=CONSOLE_LOG_FORMAT)  # DEBUG level, console stream, replaces earlier handlers
    logging.captureWarnings(True)  # routes Python warnings into the log as well
    sys.excepthook = log_uncaught_exception  # routes uncaught exceptions into the log as well

# ======================================================================================
# 5. HTTP layer
# ======================================================================================


def build_request(url: str, contact_email: str) -> urllib.request.Request:  # builds a polite HTTP request
    user_agent_text = f"expatriate-notice-downloader ({contact_email})"  # identifies this client to the server
    return urllib.request.Request(url, headers={"User-Agent": user_agent_text})  # attaches the identifying header


def fetch_bytes_with_retries(url: str, contact_email: str, maximum_attempts: int = 4, timeout_seconds: float = 60.0) -> bytes:  # downloads a URL with back-off
    global total_http_request_count  # we will modify the module level counter
    for attempt_number in itertools.count(start=1):  # counts attempts 1, 2, 3, ... until we return or raise
        with request_count_lock:  # only one thread may change the counter at a time
            total_http_request_count += 1  # records this request
            current_request_number = total_http_request_count  # copies the value while the lock is held
        failure_description = "unknown failure"  # reset before each attempt
        logging.debug(f"GET {url} (attempt {attempt_number} of {maximum_attempts}, request number {current_request_number})")  # log the outgoing request
        started_at = time.monotonic()  # start the stopwatch for this request
        try:  # attempt the request
            with HTTP_OPENER.open(build_request(url, contact_email), timeout=timeout_seconds) as http_response:  # opens the connection
                response_bytes = http_response.read()  # reads the whole body
                logging.debug(f"Response {http_response.status} {http_response.reason}, {len(response_bytes)} bytes, content type {http_response.headers.get('Content-Type')}, {time.monotonic() - started_at:.2f} seconds")  # log the response details
        except urllib.error.HTTPError as http_error:  # the server answered with an error status
            match http_error.code:  # decide what to do based on the status code
                case http.HTTPStatus.NOT_FOUND | http.HTTPStatus.GONE | http.HTTPStatus.FORBIDDEN | http.HTTPStatus.BAD_REQUEST:  # permanent problems
                    raise ExpatriateNoticeError(f"HTTP {http_error.code} for {url}") from http_error  # retrying cannot help
                case _:  # anything else, such as 429 or 5xx, may be temporary
                    failure_description = f"HTTP {http_error.code}"  # remember why we will retry
        except (urllib.error.URLError, socket.timeout, ConnectionError) as network_error:  # network level trouble
            failure_description = f"network problem: {network_error}"  # remember why we will retry
        else:  # the request succeeded
            return response_bytes  # hand the body back to the caller
        finally:  # runs after every attempt
            logging.debug(f"Attempt {attempt_number} for {url} finished")  # trace output for debugging
        if attempt_number >= maximum_attempts:  # we are out of attempts
            raise ExpatriateNoticeError(f"Giving up on {url} after {attempt_number} attempts ({failure_description})")  # report the failure
        logging.warning(f"{failure_description}; retrying {url} (attempt {attempt_number} of {maximum_attempts})")  # tell the person
        time.sleep(2 ** attempt_number)  # exponential back-off: 2, 4, 8 seconds

# ======================================================================================
# 6. Search layer (Federal Register API)
# ======================================================================================


def iterate_configured_search_terms(extra_search_terms: typing.Sequence[str]) -> typing.Iterator[str]:  # yields every term to search for
    yield from DEFAULT_SEARCH_TERMS  # first the built in term
    yield from extra_search_terms  # then any terms from the command line


def iterate_search_result_pages(search_term: str, api_documents_url: str, contact_email: str) -> typing.Iterator[dict[str, typing.Any]]:  # yields the API pages for one term
    query_parameter_pairs = [  # starts the list of query parameters
        ("conditions[term]", search_term),  # the full text search term
        ("order", "oldest"),  # a stable order makes paging predictable
        ("per_page", str(MAXIMUM_RESULTS_PER_PAGE)),  # ask for the largest page size
    ]  # ends the list
    query_parameter_pairs.extend(("fields[]", field_name) for field_name in REQUESTED_FIELD_NAMES)  # ask for only the fields we need
    next_page_url: str | None = f"{api_documents_url}?{urllib.parse.urlencode(query_parameter_pairs)}"  # the first page URL
    logging.debug(f"Searching for {search_term!r} with parameters {query_parameter_pairs}")  # log the search request
    while next_page_url is not None:  # keep going while the API offers another page
        response_bytes = fetch_bytes_with_retries(next_page_url, contact_email)  # download the page
        search_page = json.loads(response_bytes.decode("utf-8"))  # decode the JSON
        logging.debug(f"Page keys {sorted(search_page)}; count {search_page.get('count')}; total_pages {search_page.get('total_pages')}; rows {len(search_page.get('results', []))}; next_page_url {search_page.get('next_page_url')}")  # log the page envelope
        yield search_page  # hand the page to the caller and pause here
        next_page_url = search_page.get("next_page_url")  # the API gives the next page URL or null
        if not search_page.get("results"):  # an empty page means we are finished
            break  # stop the loop


def build_fallback_raw_text_url(document_number: DocumentNumber, publication_date: str) -> str:  # rebuilds the .txt URL from its known pattern
    date_path = publication_date.replace("-", "/")  # 2026-10-30 becomes 2026/10/30
    return f"https://www.federalregister.gov/documents/full_text/text/{date_path}/{document_number}.txt"  # same pattern as the example URL


def convert_row_to_document(row: dict[str, typing.Any]) -> FederalRegisterDocument:  # turns an API row into our record
    document_number = str(row["document_number"])  # the unique document identifier
    publication_date = datetime.date.fromisoformat(row["publication_date"]).isoformat()  # validates the date format
    raw_text_url = row.get("raw_text_url") or build_fallback_raw_text_url(document_number, publication_date)  # prefer the API URL
    logging.debug(f"Row {document_number}: date {publication_date}, title {row.get('title')!r}, url {raw_text_url} (fallback used: {not row.get('raw_text_url')})")  # log every row
    return FederalRegisterDocument(document_number, str(row.get("title") or ""), publication_date, raw_text_url)  # builds the record


def collect_documents_for_search_term(search_term: str, api_documents_url: str, contact_email: str) -> list[FederalRegisterDocument]:  # gathers every result for one term
    collected_rows: list[dict[str, typing.Any]] = []  # raw rows from all pages
    reported_count = 0  # the total the API says exists
    maximum_page_count = 1  # how many pages we expect
    for page_index, search_page in enumerate(iterate_search_result_pages(search_term, api_documents_url, contact_email), start=1):  # walk the pages
        if page_index == 1:  # only the first page tells us the total
            reported_count = int(search_page.get("count", 0))  # read the reported total
            if reported_count >= API_RESULT_CEILING:  # the API cannot be trusted at this size
                raise ExpatriateNoticeError(f"Search '{search_term}' hit the {API_RESULT_CEILING} result ceiling; narrow it by date")  # stop loudly
            maximum_page_count = max(1, math.ceil(reported_count / MAXIMUM_RESULTS_PER_PAGE))  # pages needed at the page size
        collected_rows.extend(search_page.get("results", []))  # keep this page's rows
        if page_index >= maximum_page_count:  # never trust "until empty" paging
            break  # we have asked for every page we need
    assert len(collected_rows) == reported_count, f"Collected {len(collected_rows)} rows but the API reported {reported_count}"  # catches silent paging problems
    logging.info(f"Search {search_term!r} returned {reported_count} documents")  # progress message
    return [convert_row_to_document(row) for row in collected_rows]  # convert every row


def sort_documents_oldest_first(documents_by_number: dict[DocumentNumber, FederalRegisterDocument]) -> list[FederalRegisterDocument]:  # sorts every search hit, with no filtering
    sorted_documents = sorted(documents_by_number.values(), key=lambda document: (document.publication_date, document.document_number))  # oldest first
    for document in sorted_documents:  # log every document that will be downloaded
        logging.debug(f"Queued {document.document_number} ({document.publication_date}): {document.title}")  # one line per document
    return sorted_documents  # hand back the sorted list

# ======================================================================================
# 7. Download layer
# ======================================================================================


def build_destination_file_name(document: FederalRegisterDocument) -> str:  # decides the local file name
    raw_file_name = f"{document.publication_date}_{document.document_number}.txt"  # date first so folders sort chronologically
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw_file_name)  # replaces any unsafe character


def download_one_document(document: FederalRegisterDocument, output_directory: pathlib.Path, contact_email: str) -> DownloadOutcome:  # downloads one file (blocking)
    destination_path = output_directory / build_destination_file_name(document)  # where the file will live
    if destination_path.is_file() and destination_path.stat().st_size > 0:  # already downloaded and not empty
        logging.info(f"Skipping {destination_path.name} because it already exists")  # tell the person
        return DownloadOutcome.SKIPPED_EXISTING  # nothing more to do
    temporary_path: pathlib.Path | None = None  # remembers the temporary file for cleanup
    try:  # attempt the download
        document_bytes = fetch_bytes_with_retries(document.raw_text_url, contact_email)  # fetch the .txt content
        if not document_bytes:  # an empty body is a failure
            raise ExpatriateNoticeError(f"Empty response for {document.raw_text_url}")  # report it
        with tempfile.NamedTemporaryFile(mode="wb", dir=output_directory, suffix=".part", delete=False) as temporary_file:  # write beside the target
            temporary_path = pathlib.Path(temporary_file.name)  # remember the temporary name
            temporary_file.write(document_bytes)  # save the content
        logging.debug(f"Wrote {len(document_bytes)} bytes to temporary file {temporary_path.name}")  # log the temporary write
        os.replace(temporary_path, destination_path)  # atomically move it into place, so partial files never look complete
        logging.debug(f"Moved {temporary_path.name} to {destination_path}")  # log the final move
    except (ExpatriateNoticeError, OSError) as download_error:  # expected failures
        logging.error(f"Failed {document.document_number}: {download_error}")  # report the failure
        return DownloadOutcome.FAILED  # tell the caller
    else:  # everything worked
        checksum_prefix = hashlib.sha256(document_bytes).hexdigest()[:12]  # short checksum for the log
        logging.info(f"Downloaded {destination_path.name} ({len(document_bytes)} bytes, sha256 {checksum_prefix})")  # report success
        return DownloadOutcome.DOWNLOADED  # tell the caller
    finally:  # always runs
        if temporary_path is not None:  # a temporary file was created
            with contextlib.suppress(FileNotFoundError):  # it is already gone after a successful move
                temporary_path.unlink()  # remove any leftover partial file


async def download_all_documents(documents: list[FederalRegisterDocument], output_directory: pathlib.Path, contact_email: str, maximum_concurrent_downloads: int, delay_seconds: float) -> collections.Counter[DownloadOutcome]:  # downloads everything concurrently
    concurrency_limiter = asyncio.Semaphore(maximum_concurrent_downloads)  # caps simultaneous downloads
    outcome_counter: collections.Counter[DownloadOutcome] = collections.Counter()  # tallies the results
    completed_count = 0  # how many documents are finished
    blocking_download = functools.partial(download_one_document, output_directory=output_directory, contact_email=contact_email)  # pre-fills the shared arguments

    async def download_with_limit(document: FederalRegisterDocument) -> None:  # one task per document
        nonlocal completed_count  # we update the counter of the enclosing function
        async with concurrency_limiter:  # wait for a free slot
            outcome = await asyncio.to_thread(blocking_download, document)  # run the blocking download in a thread
            if outcome is DownloadOutcome.DOWNLOADED:  # only real downloads need a polite pause
                await asyncio.sleep(delay_seconds)  # be gentle with the server
        completed_count += 1  # one more document finished
        outcome_counter[outcome] += 1  # tally the outcome
        logging.info(f"Progress: {completed_count} of {len(documents)} documents handled")  # progress message

    await asyncio.gather(*(download_with_limit(document) for document in documents))  # run all tasks and wait for them
    return outcome_counter  # hand back the tally

# ======================================================================================
# 8. Command line and main
# ======================================================================================


def parse_command_line_arguments(argument_list: typing.Sequence[str] | None) -> argparse.Namespace:  # defines the command line
    argument_parser = argparse.ArgumentParser(description="Download Federal Register 6039G expatriate notices as .txt files.")  # creates the parser
    argument_parser.add_argument("--output-directory", default="TXTs", help="folder for the downloaded files (default: TXTs)")  # where to save
    argument_parser.add_argument("--contact-email", default="you@example.com", help="your email, sent in the User-Agent header")  # polite identification
    argument_parser.add_argument("--api-documents-url", default=API_DOCUMENTS_URL, help="override the search endpoint (useful for testing)")  # test hook
    argument_parser.add_argument("--extra-search-term", action="append", default=[], help="an additional search term, repeatable")  # more searches
    argument_parser.add_argument("--maximum-concurrent-downloads", type=int, default=3, help="simultaneous downloads (default: 3)")  # concurrency
    argument_parser.add_argument("--delay-seconds", type=float, default=1.0, help="pause after each download (default: 1.0)")  # politeness
    argument_parser.add_argument("--dry-run", action="store_true", help="list the matching documents without downloading")  # list only
    return argument_parser.parse_args(argument_list)  # parses and returns the arguments


def main(argument_list: typing.Sequence[str] | None = None) -> int:  # the program entry point
    configure_console_logging()  # everything below is logged to the console
    arguments = parse_command_line_arguments(argument_list)  # read the command line
    logging.debug(f"Python {sys.version.split()[0]} on {sys.platform}; arguments: {vars(arguments)}")  # log the environment and every argument
    output_directory = pathlib.Path(arguments.output_directory)  # the target folder
    output_directory.mkdir(parents=True, exist_ok=True)  # create it when missing
    logging.debug(f"Output directory is {output_directory.resolve()}")  # log the absolute path
    try:  # searching can fail
        documents_per_term = [  # one list of documents per search term
            collect_documents_for_search_term(search_term, arguments.api_documents_url, arguments.contact_email)  # run one search
            for search_term in iterate_configured_search_terms(arguments.extra_search_term)  # for every configured term
        ]  # ends the list
    except ExpatriateNoticeError as search_error:  # an expected failure
        logging.error(f"Search failed: {search_error}")  # tell the person
        return 2  # exit code for search failures
    documents_by_number = {document.document_number: document for document in itertools.chain.from_iterable(documents_per_term)}  # merges and removes duplicates
    logging.debug(f"{len(documents_by_number)} unique documents after merging all searches")  # log the merge result
    del documents_per_term  # the per-term lists are no longer needed
    matching_documents = sort_documents_oldest_first(documents_by_number)  # sort every search hit
    logging.info(f"{len(matching_documents)} matching documents found")  # progress message
    if not matching_documents:  # nothing to do
        logging.warning("No matching documents were found.")  # tell the person
        return 1  # exit code for "nothing found"
    if arguments.dry_run:  # listing only
        for document in matching_documents:  # log each document
            logging.info(f"Dry run: {document.publication_date}  {document.document_number}  {document.raw_text_url}")  # one line per document
        return 0  # success
    outcome_counter = asyncio.run(download_all_documents(matching_documents, output_directory, arguments.contact_email, arguments.maximum_concurrent_downloads, arguments.delay_seconds), debug=True)  # download everything with asyncio debug logging
    for outcome in DownloadOutcome:  # log every outcome, even when zero
        logging.info(f"Summary - {outcome.value}: {outcome_counter[outcome]}")  # one line per outcome
    logging.info(f"Summary - HTTP requests made: {total_http_request_count}")  # shows the global counter
    return 1 if outcome_counter[DownloadOutcome.FAILED] else 0  # non-zero exit code when anything failed


if __name__ == "__main__":  # only when run as a script
    raise SystemExit(main())  # exit with the return value of main
