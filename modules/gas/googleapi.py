#!python3

"""Functions for working with the Google API"""
import os
import json
import pickle
import gspread
from datetime import datetime
from googleapiclient import errors
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.transport.requests import AuthorizedSession
from modules.gas import filework


"""
###########################################################################
# This section here is for working with and managing credentials and services
###########################################################################
"""


def get_credential_project(cfg):
    """Retrieves the project name for the current credentials"""
    secret_path = os.path.join(cfg["store_dir"], cfg["credentials_file"])
    cred_data = json.loads(filework.grab_file_as_text(secret_path))
    return cred_data["installed"]["project_id"]


def get_credentials(cfg):
    """
    Gets valid user credentials from storage.

    If nothing has been stored or if the stored credentials are invalid,
    will run an authorization flow (command line) to obtain new credentials.

    Assumes store_file is/will be and secret_file is in store_dir
    These and other constants passed via cfg dict from a startup yaml file

    Returns:
        credentials, the obtained credential.
    """
    credential_path = os.path.join(cfg["store_dir"], cfg["credentials_store"])
    credentials = None
    if os.path.exists(credential_path):
        with open(credential_path, "rb") as token:
            credentials = pickle.load(token)

    cfg["logger"].debug("Getting credentials", sub="get_credentials")

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            secret_path = os.path.join(cfg["store_dir"], cfg["credentials_file"])
            flow = InstalledAppFlow.from_client_secrets_file(secret_path, cfg["scopes"])
            credentials = flow.run_local_server()
        with open(credential_path, "wb") as token:
            pickle.dump(credentials, token)

    return credentials


def get_service(service_type, version, creds, cfg):
    """Requests a service from the Google API"""
    cfg["logger"].debug(
        "Getting service", sub="get_service", service_type=service_type, version=version
    )
    try:
        return build(service_type, version, credentials=creds)
    except AttributeError as e:
        cfg["logger"].warning(
            "Credentials attribute error",
            sub="get_credentials",
            **{k: str(v) for k, v in creds.items()}
        )
        raise e


class ScriptSettings(object):
    """
    Class to hold local settings and construct/save locally
    to a YAML file
    """

    def __init__(self, cfg, scriptId="", apiId=""):
        self.local_settings = cfg["local_settings"]
        if os.path.exists(self.local_settings):
            self.settings = filework.grab_yaml(self.local_settings)
        else:
            self.settings = {
                "scriptId": scriptId,
                "API_ID": apiId,
            }

    def __repr__(self):
        return "scriptId: " + self.settings["scriptId"]

    def set_api_id(self, id):
        self.settings["API_ID"] = id

    def get_script_id(self):
        return self.settings["scriptId"]

    def get_api_id(self):
        return self.settings["API_ID"]

    def store(self):
        filework.store_yaml(self.local_settings, self.settings)


class Creds(object):
    """Class to house credentials and manage timeouts"""

    def __init__(self, cfg):
        self._creds = get_credentials(cfg)
        self._refresh_ttl = cfg["refresh_ttl"]
        self._versions = cfg["service_versions"]
        self.project = get_credential_project(cfg)

    def cred(self):
        """Returns the class variable, refreshing if close to expiring"""
        if self._creds.expiry:
            expires_in = (self._creds.expiry - datetime.utcnow()).total_seconds()
            if expires_in < self._refresh_ttl:
                self._creds = self._creds.refresh(Request())
        return self._creds

    def gspread_client(self):
        """
        Returns a gspread client object.
        Google has deprecated Oauth2, but the gspread library still uses the creds
        from that system, so this function bypasses the regular approach and creates
        and authorizes the client here instead.
        Code copied from answer here: https://github.com/burnash/gspread/issues/472
        """
        regular_cred = self.cred()
        gc = gspread.Client(auth=regular_cred)
        gc.session = AuthorizedSession(regular_cred)
        return gc

    def serv(self, service_type, cfg):
        return get_service(service_type, self._versions[service_type], self.cred(), cfg)


"""
###########################################################################
# This section here is for actually interacting with the apis
###########################################################################
"""


def move_file(id, folder, drive_service, cfg):
    """Moves a Google Drive object, id, to the Drive folder"""
    cfg["logger"].debug(
        "Moving Drive file to folder", sub="move_file", file=id, folder=folder
    )
    this_file = drive_service.files().get(fileId=id, fields="parents").execute()
    previous_parents = ",".join(this_file.get("parents"))
    drive_service.files().update(
        fileId=id,
        addParents=folder,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()


def add_link_permissions(id, drive_service, cfg, allow="writer"):
    """
    Opens up permissions to a file to allow anyone with link to edit by default. If
    allow='reader', the change is to view.
    """
    cfg["logger"].debug(
        "Opening up permissions for file",
        sub="add_link_permissions",
        id=id,
        allow=allow,
    )
    file_permission = {"role": allow, "type": "anyone", "withLink": True}
    drive_service.permissions().create(
        fileId=id, body=file_permission, fields="id"
    ).execute()


def output_script_error(error, cfg):
    """
    Cleanly outputs an error return from an Apps Script API call
    """
    cfg["logger"].error("Script error", error_message=str(error["errorMessage"]))
    if "scriptStackTraceElements" in error:
        cfg["logger"].error("Script error stracktrace follows")
        for trace in error["scriptStackTraceElements"]:
            cfg["logger"].error(
                "\t%s: %s" % (str(trace["function"]), str(trace["lineNumber"]))
            )
    return "Error"


def call_apps_script(
    request, script_service, cfg, error_handler=output_script_error, dev_mode="true"
):
    """
    Helper function to call app_script and manage any errors.
    dev_mode being causes code to run on last save and not last deployment.
    """
    script_id = ScriptSettings(cfg).get_script_id()
    request["devMode"] = dev_mode  # runs latest save if true (vs deployment)
    cfg["logger"].debug(
        "Calling Apps Script",
        sub="call_apps_script",
        **{k: str(v) for k, v in request.items() if k != "parameters"}
    )
    try:
        response = (
            script_service.scripts().run(body=request, scriptId=script_id).execute()
        )
        if "error" in response:
            # Here, the script is returning an error
            return error_handler(response["error"]["details"][0], cfg)
        else:
            return response["response"].get("result", {})

    except errors.HttpError as e:
        # Here, the script likely didn't execute: there was an error prior
        cfg["logger"].error(e.content)
        raise


"""
###########################################################################
# This section here is for convenience functions with the gspread library
###########################################################################
"""


def write_lol_to_sheet(
    ws, lol_data, cfg, na_val="", resize=True, value_input_option="USER_ENTERED"
):
    """
    Uses write ranges to write a list of lists (lol) of data to a google sheet.
    Efficient for sparse data because it only writes cells that are not ''.
    Because None doesn't write to JSON well, converts null to na_val.
    The default for value_input_option tells Sheets to calculate formulas.
    """
    n_rows = len(lol_data)
    n_cols = len(lol_data[0])
    cfg["logger"].info(
        "Writing data to sheet",
        sub="write_lol_to_sheet",
        rows=n_rows,
        cols=n_cols,
        sheet=ws.title,
    )
    if resize:
        ws.resize(rows=n_rows, cols=n_cols)

    lol_clean = [[na_val if x is None else x for x in row] for row in lol_data]

    write_range = ws.range(1, 1, n_rows, n_cols)
    serial_data = [item for row in lol_clean for item in row]

    # Now either pop cells from the range or map the data (working backwards for index)
    for i in range(len(serial_data) - 1, -1, -1):
        if serial_data[i] == "":
            write_range.pop(i)
        else:
            write_range[i].value = serial_data[i]

    ws.update_cells(write_range, value_input_option)


def send_bulk_data(ws, output_matrix, cfg, value_input_option="USER_ENTERED"):
    """
    Takes a list of tuples (r, c, value) and pushes it to the
    update_cells method instead of doing individual updates.
    r and c values start with 1 because this a reference to Google Sheets.
    This optimizes the number of read and write requests to the Google API
    by first getting a range bound by the min and max coordinates and then
    popping them from the list prior to writing.
    The default for value_input_option tells Sheets to calculate formulas.
    """
    cfg["logger"].info(
        "Writing cell data to sheet",
        sub="send_bulk_data",
        cells=len(output_matrix),
        sheet=ws.title,
    )
    rs = [x[0] for x in output_matrix]
    cs = [x[1] for x in output_matrix]
    cells = {str(x[0]) + "," + str(x[1]): x[2] for x in output_matrix}
    write_range = ws.range(min(rs), min(cs), max(rs), max(cs))
    for i in range(len(write_range) - 1, -1, -1):
        loc = str(write_range[i].row) + "," + str(write_range[i].col)
        if loc in cells:
            write_range[i].value = cells[loc]
        else:
            write_range.pop(i)

    ws.update_cells(write_range, value_input_option=value_input_option)
