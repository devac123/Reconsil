import os
import shutil

# Current directory
parent_dir = os.getcwd()

# Keep track of copied filenames
copied_files = {}

for root, dirs, files in os.walk(parent_dir):
    # Skip the parent directory itself
    if root == parent_dir:
        continue

    for file in files:
        if file.lower().endswith(".html"):
            source_path = os.path.join(root, file)

            filename, extension = os.path.splitext(file)
            destination_name = file

            # Handle duplicate filenames
            if destination_name in copied_files:
                copied_files[destination_name] += 1
                destination_name = f"{filename}_{copied_files[file]}{extension}"
            else:
                copied_files[destination_name] = 0

            destination_path = os.path.join(parent_dir, destination_name)

            shutil.copy2(source_path, destination_path)
            print(f"Copied: {source_path} -> {destination_path}")

print("Done! All HTML files have been copied to the current folder.")