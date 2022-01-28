# coding=utf-8
from __future__ import absolute_import
from __future__ import unicode_literals

import octoprint.plugin
import octoprint.filemanager
import octoprint.filemanager.util
import octoprint.util
import os
import datetime
import io
from PIL import Image


class PrusaslicerthumbnailsPlugin(octoprint.plugin.SettingsPlugin,
								  octoprint.plugin.AssetPlugin,
								  octoprint.plugin.TemplatePlugin,
								  octoprint.plugin.EventHandlerPlugin,
								  octoprint.plugin.SimpleApiPlugin):

	def __init__(self):
		self._fileRemovalTimer = None
		self._fileRemovalLastDeleted = None
		self._fileRemovalLastAdded = None
		self._folderRemovalTimer = None
		self._folderRemovalLastDeleted = {}
		self._folderRemovalLastAdded = {}
		self._waitForAnalysis = False
		self._analysis_active = False

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			installed=True,
			inline_thumbnail=False,
			scale_inline_thumbnail=False,
			inline_thumbnail_scale_value="50",
			inline_thumbnail_position_left=False,
			align_inline_thumbnail=False,
			inline_thumbnail_align_value="left",
			state_panel_thumbnail=True,
			state_panel_thumbnail_scale_value="100",
			resize_filelist=False,
			filelist_height="306",
			scale_inline_thumbnail_position=False
		)

	##~~ AssetPlugin mixin

	def get_assets(self):
		return dict(
			js=["js/prusaslicerthumbnails.js"],
			css=["css/prusaslicerthumbnails.css"]
		)

	##~~ TemplatePlugin mixin

	def get_template_configs(self):
		return [
			dict(type="settings", custom_bindings=False, template="prusaslicerthumbnails_settings.jinja2"),
		]

	def _extract_thumbnail(self, gcode_filename, thumbnail_filename):
		import re
		import base64
		regex = r"(?:^; thumbnail begin \d+[x ]\d+ \d+)(?:\n|\r\n?)((?:.+(?:\n|\r\n?))+?)(?:^; thumbnail end)"
		regex_mks = re.compile('(?:;(?:simage|;gimage):).*?M10086 ;[\r\n]', re.DOTALL)
		lineNum = 0
		collectedString = ""
		use_mks = False
		with open(gcode_filename, "rb") as gcode_file:
			for line in gcode_file:
				lineNum += 1
				line = line.decode("utf-8", "ignore")
				gcode = octoprint.util.comm.gcode_command_for_cmd(line)
				extrusionMatch = octoprint.util.comm.regexes_parameters["floatE"].search(line)
				if gcode == "G1" and extrusionMatch:
					self._logger.debug("Line %d: Detected first extrusion. Read complete.", lineNum)
					break
				if line.startswith(";") or line.startswith("\n") or line.startswith("M10086 ;"):
					collectedString += line
			self._logger.debug(collectedString)
			test_str = collectedString.replace(octoprint.util.to_native_str('\r\n'), octoprint.util.to_native_str('\n'))
		test_str = test_str.replace(octoprint.util.to_native_str(';\n;\n'), octoprint.util.to_native_str(';\n\n;\n'))
		matches = re.findall(regex, test_str, re.MULTILINE)
		if len(matches) == 0:  # MKS lottmaxx fallback
			matches = regex_mks.findall(test_str)
			if len(matches) > 0:
				use_mks = True
		if len(matches) > 0:
			maxlen=0
			choosen=-1
			for i in range(len(matches)):
				curlen=len(matches[i])
				if maxlen<curlen:
					maxlen=curlen
					choosen=i
			path = os.path.dirname(thumbnail_filename)
			if not os.path.exists(path):
				os.makedirs(path)
			with open(thumbnail_filename, "wb") as png_file:
				if use_mks:
					png_file.write(self._extract_mks_thumbnail(matches))
				else:
					png_file.write(base64.b64decode(matches[choosen].replace("; ", "").encode()))

	# Extracts a thumbnail from a gcode and returns png binary string
	def _extract_mks_thumbnail(self, gcode_encoded_images):

		# Find the biggest thumbnail
		encoded_image_dimensions, encoded_image = self.find_best_thumbnail(gcode_encoded_images)

		# Not found?
		if encoded_image is None:
			return None  # What to return? Is None ok?

		# Remove M10086 ; and whitespaces
		encoded_image = encoded_image.replace('M10086 ;', '').replace('\n', '').replace('\r', '').replace(' ', '')

		# Get bytes from hex
		encoded_image = bytes(bytearray.fromhex(encoded_image))

		# Load pixel data
		image = Image.frombytes('RGB', encoded_image_dimensions, encoded_image, 'raw', 'BGR;16', 0, 1)

		# Save image as png
		with io.BytesIO() as png_bytes:
			image.save(png_bytes, "PNG")
			png_bytes_string = png_bytes.getvalue()

		return png_bytes_string

	# Finds the biggest thumbnail
	def find_best_thumbnail(self, gcode_encoded_images):

		# Check for gimage
		for image in gcode_encoded_images:
			if image.startswith(';;gimage:'):
				# Return size and trimmed string
				return (200, 200), image[9:]

		# Check for simage
		for image in gcode_encoded_images:
			if image.startswith(';simage:'):
				# Return size and trimmed string
				return (100, 100), image[8:]

		# Image not found
		return None

	##~~ EventHandlerPlugin mixin

	def on_event(self, event, payload):
		if event not in ["FileAdded", "FileRemoved", "FolderRemoved", "FolderAdded"]:
			return
		if event == "FolderRemoved" and payload["storage"] == "local":
			import shutil
			shutil.rmtree(self.get_plugin_data_folder() + "/" + payload["path"], ignore_errors=True)
		if event == "FolderAdded" and payload["storage"] == "local":
			file_list = self._file_manager.list_files(path=payload["path"], recursive=True)
			local_files = file_list["local"]
			results = dict(no_thumbnail=[], no_thumbnail_src=[])
			for file_key, file in local_files.items():
				results = self._process_gcode(local_files[file_key], results)
			self._logger.debug("Scan results: {}".format(results))
		if event in ["FileAdded", "FileRemoved"] and payload["storage"] == "local" and "gcode" in payload["type"]:
			thumbnail_filename = self.get_plugin_data_folder() + "/" + payload["path"].replace(".gcode", ".png")
			if os.path.exists(thumbnail_filename):
				os.remove(thumbnail_filename)
			if event == "FileAdded":
				gcode_filename = self._file_manager.path_on_disk("local", payload["path"])
				self._extract_thumbnail(gcode_filename, thumbnail_filename)
				if os.path.exists(thumbnail_filename):
					thumbnail_url = "plugin/prusaslicerthumbnails/thumbnail/" + payload["path"].replace(".gcode",
																										".png") + "?" + "{:%Y%m%d%H%M%S}".format(
						datetime.datetime.now())
					self._file_manager.set_additional_metadata("local", payload["path"], "thumbnail",
															   thumbnail_url.replace("//", "/"), overwrite=True)
					self._file_manager.set_additional_metadata("local", payload["path"], "thumbnail_src",
															   self._identifier, overwrite=True)

	##~~ SimpleApiPlugin mixin

	def _process_gcode(self, gcode_file, results=[]):
		self._logger.debug(gcode_file["path"])
		if gcode_file.get("type") == "machinecode":
			self._logger.debug(gcode_file.get("thumbnail"))
			if gcode_file.get("thumbnail") is None or not os.path.exists(gcode_file.get("thumbnail").split("?")[0]):
				self._logger.debug("No Thumbnail for %s, attempting extraction" % gcode_file["path"])
				results["no_thumbnail"].append(gcode_file["path"])
				self.on_event("FileAdded", dict(path=gcode_file["path"], storage="local", type=["gcode"]))
			elif "prusaslicerthumbnails" in gcode_file.get("thumbnail") and not gcode_file.get("thumbnail_src"):
				self._logger.debug("No Thumbnail source for %s, adding" % gcode_file["path"])
				results["no_thumbnail_src"].append(gcode_file["path"])
				self._file_manager.set_additional_metadata("local", gcode_file["path"], "thumbnail_src",
														   self._identifier, overwrite=True)
		elif gcode_file.get("type") == "folder" and not gcode_file.get("children") == None:
			children = gcode_file["children"]
			for key, file in children.items():
				self._process_gcode(children[key], results)
		return results

	def get_api_commands(self):
		return dict(crawl_files=[])

	def on_api_command(self, command, data):
		import flask
		import json
		from octoprint.server import user_permission
		if not user_permission.can():
			return flask.make_response("Insufficient rights", 403)

		if command == "crawl_files":
			self._logger.debug("Crawling Files")
			FileList = self._file_manager.list_files(recursive=True)
			self._logger.info(FileList)
			LocalFiles = FileList["local"]
			results = dict(no_thumbnail=[], no_thumbnail_src=[])
			for key, file in LocalFiles.items():
				results = self._process_gcode(LocalFiles[key], results)
			return flask.jsonify(results)

	##~~ Routes hook
	def route_hook(self, server_routes, *args, **kwargs):
		from octoprint.server.util.tornado import LargeResponseHandler, UrlProxyHandler, path_validation_factory
		from octoprint.util import is_hidden_path
		return [
			(r"thumbnail/(.*)", LargeResponseHandler, dict(path=self.get_plugin_data_folder(),
														   as_attachment=False,
														   path_validation=path_validation_factory(
															   lambda path: not is_hidden_path(path), status_code=404)))
		]

	##~~ Softwareupdate hook

	def get_update_information(self):
		return dict(
			prusaslicerthumbnails=dict(
				displayName="Slicer Thumbnails",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="jneilliii",
				repo="OctoPrint-PrusaSlicerThumbnails",
				current=self._plugin_version,
				stable_branch=dict(
					name="Stable", branch="master", comittish=["master"]
				),
				prerelease_branches=[
					dict(
						name="Release Candidate",
						branch="rc",
						comittish=["rc", "master"],
					)
				],

				# update method: pip
				pip="https://github.com/jneilliii/OctoPrint-PrusaSlicerThumbnails/archive/{target_version}.zip"
			)
		)


__plugin_name__ = "Slicer Thumbnails"
__plugin_pythoncompat__ = ">=2.7,<4"  # python 2 and 3


def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PrusaslicerthumbnailsPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
		"octoprint.server.http.routes": __plugin_implementation__.route_hook
	}
