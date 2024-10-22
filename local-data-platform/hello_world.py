from local_data_platform.hello_world import hello_world

hello_world()

import sys
print(sys.path)

import pkg_resources

# Example usage
version = pkg_resources.get_distribution("sphinx_rtd_theme").version
print(version)

import sphinx_rtd_theme
# print(sphinx_rtd_theme.__file__)
#
#
# print(sphinx_rtd_theme.get_html_theme_path())

import os
import sys
print(sys.path)

# Print the installation path of sphinx_rtd_theme
print("sphinx_rtd_theme path:", sphinx_rtd_theme.__file__)


# Get the theme path
theme_path = sphinx_rtd_theme.get_html_theme_path()
print("Theme path:", theme_path)

# Check if theme.html exists
html_file = os.path.join(theme_path[0], 'theme.html')  # Assuming the first path is what you need
print("theme.html exists:", os.path.exists(html_file))

# print(pkg_resources.resource_exists('sphinx_rtd_theme', 'theme.html'))
