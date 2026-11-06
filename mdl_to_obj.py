import struct
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox

# Import CustomTkinter for the graphical user interface.
# If it is not installed, display instructions for installing it.
try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit("Install CustomTkinter: py -m pip install customtkinter")


# MDL file signature used to identify valid files.
SIGNATURE = b"\x89YO3"

# Block type identifiers recognised by the MDL parser.
KNOWN_TYPES = {
    0x02, 0x03, 0x0A, 0x0C, 0x14,
    0x19, 0x28, 0x29, 0x46
}

# Characters permitted in block and material names.
# These are used when scanning the binary file for block headers.
NAME_BYTES = set(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.\\_"
)


# Custom exception used for errors encountered while parsing MDL files.
class FormatError(Exception):
    pass


# Represents an individual block discovered inside the MDL file.
@dataclass
class Block:
    kind: int       # Numeric identifier describing the block type.
    name: str       # Name associated with the block.
    payload: bytes  # Raw binary data contained in the block.


# Stores material information extracted from the MDL file.
@dataclass
class Material:
    name: str
    texture: str = ""  # Optional texture filename associated with the material.


# Stores geometry and associated information for an individual mesh.
@dataclass
class Mesh:
    name: str
    model: str
    material: str

    # Lists are created separately for each mesh using default_factory.
    vertices: list = field(default_factory=list)
    uvs: list = field(default_factory=list)
    faces: list = field(default_factory=list)


# Represents the complete parsed MDL document.
@dataclass
class MdlDocument:
    path: Path       # Original MDL file location.
    materials: dict  # Dictionary containing extracted materials.
    meshes: list     # Collection of extracted meshes.
    warnings: list   # Parsing warnings and errors that did not stop processing.


def valid_name_run(data, pos):
    """
    Count consecutive bytes that are valid characters for a block name.

    This helps identify potential block headers when scanning the file.
    """
    end = pos

    # Continue until an invalid character or the end of the data is reached.
    while end < len(data) and data[end] in NAME_BYTES:
        end += 1

    return end - pos


def discover_blocks(data):
    """
    Locate and extract recognised blocks from the binary MDL file.

    Each block consists of:
        - A one-byte block type.
        - A one-byte name length.
        - The block name.
        - The remaining block payload.

    The function scans the file for valid block headers and uses the next
    recognised header to determine where the current payload ends.
    """

    # Check the file signature and minimum expected header size.
    if len(data) < 11 or data[:4] != SIGNATURE:
        raise FormatError("Invalid MDL file.")

    blocks = []

    # Skip the initial 11-byte file header.
    offset = 11

    # Continue scanning while there is enough data for a block header.
    while offset + 2 <= len(data):
        kind = data[offset]       # Read the block type.
        n = data[offset + 1]      # Read the block name length.

        # Ignore this position if the header appears invalid.
        if (
            kind not in KNOWN_TYPES
            or n < 2
            or offset + 2 + n > len(data)
        ):
            offset += 1
            continue

        # Extract the potential block name.
        name_bytes = data[offset + 2:offset + 2 + n]

        # Ensure every character in the name is valid.
        if any(c not in NAME_BYTES for c in name_bytes):
            offset += 1
            continue

        # Decode the block name as ASCII.
        name = name_bytes.decode("ascii")

        # The payload begins immediately after the block name.
        payload_start = offset + 2 + n
        scan = payload_start

        # Assume the payload continues to the end of the file
        # unless another valid block header is discovered.
        next_header = len(data)

        # Search forward for the next potential block header.
        while scan + 2 < len(data):
            run = valid_name_run(data, scan + 2)

            # Check whether the current position could contain a valid header.
            if (
                data[scan] in KNOWN_TYPES
                and 2 <= data[scan + 1] <= min(run, 255)
                and scan + 2 + data[scan + 1] <= len(data)
            ):
                candidate = data[
                    scan + 2:scan + 2 + data[scan + 1]
                ]

                # Confirm that the potential block name contains valid bytes.
                if all(c in NAME_BYTES for c in candidate):
                    next_header = scan
                    break

            # Move forward one byte and continue searching.
            scan += 1

        # Save the block and its extracted payload.
        blocks.append(
            Block(
                kind,
                name,
                data[payload_start:next_header]
            )
        )

        # Continue scanning from the next discovered block.
        offset = next_header

        # Stop when the end of the file has been reached.
        if offset == len(data):
            break

    # Reject files where no recognised blocks were discovered.
    if not blocks:
        raise FormatError("No recognised MDL blocks found.")

    return blocks


def unpack_u16(buf, off):
    """
    Read an unsigned 16-bit integer from a byte buffer.

    '<H' specifies a little-endian unsigned short (2 bytes).
    """
    if off + 2 > len(buf):
        raise FormatError("Unexpected end of shape block.")

    return struct.unpack_from("<H", buf, off)[0]


def parse_shape(block, model, material):
    """
    Decode a shape block into a Mesh object.

    Extracts:
        - Vertex positions.
        - Face descriptors.
        - UV coordinates.
        - Triangulated face indices.

    Returns the mesh alongside the stored face count and total
    number of vertices described by the face data.
    """
    b = block.payload
    off = 0

    # Read the number of vertices stored in the shape.
    nv = unpack_u16(b, off)
    off += 2

    # Prevent unreasonable vertex counts and ensure enough binary data exists.
    if nv > 2000000 or off + nv * 12 > len(b):
        raise FormatError(f"{block.name}: invalid vertex count.")

    # Each vertex contains three 32-bit floating-point values (X, Y, Z).
    # '<3f' means three little-endian floats per vertex.
    vertices = list(
        struct.iter_unpack("<3f", b[off:off + nv * 12])
    )
    off += nv * 12

    # Read the stored face count.
    nf = unpack_u16(b, off)
    off += 2

    # Face descriptors describe how many vertices belong to each polygon.
    descriptors = []
    total = 0

    # Read descriptors until the UV marker (0x16) is encountered.
    while off < len(b) and b[off] != 0x16:
        d = b[off]
        off += 1

        # Upper bits encode the polygon vertex count minus one.
        count = (d >> 3) + 1

        # Lower three bits encode the descriptor width.
        width = d & 7

        # Only the supported descriptor widths are accepted.
        if width not in (3, 4, 5, 6):
            raise FormatError(
                f"{block.name}: invalid face descriptor."
            )

        descriptors.append((count, width))
        total += count

    # Make sure the UV marker was found.
    if off >= len(b):
        raise FormatError(f"{block.name}: missing UV marker.")

    # Skip the UV marker.
    off += 1

    # Read the number of UV coordinates.
    uv_count = unpack_u16(b, off)
    off += 2

    # This format expects one UV coordinate for every vertex.
    if uv_count != nv:
        raise FormatError(f"{block.name}: UV count mismatch.")

    # Each UV coordinate consists of two 32-bit floating-point values.
    if off + nv * 8 > len(b):
        raise FormatError(f"{block.name}: truncated UV data.")

    # Decode the UV coordinates as pairs of floats.
    uvs = list(
        struct.iter_unpack("<2f", b[off:off + nv * 8])
    )

    # Convert polygon data into triangles for OBJ compatibility.
    faces = []
    cursor = 0

    # Process each polygon descriptor.
    for count, width in descriptors:

        # Process each polygon described by this descriptor.
        for _ in range(count):

            # Ensure the polygon does not reference vertices beyond the mesh.
            if cursor + width > nv:
                raise FormatError(
                    f"{block.name}: invalid face indices."
                )

            # Triangulate the polygon using a triangle fan.
            # The first vertex is shared by every generated triangle.
            for j in range(1, width - 1):
                faces.append(
                    (cursor, cursor + j + 1, cursor + j)
                )

            # Move to the beginning of the next polygon.
            cursor += width

    # Validate the final number of consumed vertex indices.
    if cursor > nv:
        raise FormatError(f"{block.name}: invalid face stream.")

    # Create the mesh using the extracted geometry and assigned material.
    mesh = Mesh(
        block.name,
        model,
        material,
        vertices,
        uvs,
        faces
    )

    return mesh, nf, total


def parse_mdl(path):
    """
    Read and parse an entire MDL file.

    Discovers blocks, extracts materials, identifies models,
    parses shape geometry and collects any warnings encountered.
    """

    # Read the file as raw bytes and discover its recognised blocks.
    blocks = discover_blocks(path.read_bytes())

    materials = {}
    meshes = []
    warnings = []

    # Use the filename as the default model name.
    current_model = path.stem

    # Process every discovered block in file order.
    for i, block in enumerate(blocks):

        # ---------------------------------------------------------
        # Material block
        # ---------------------------------------------------------
        if block.kind == 2:

            # Ignore material blocks that are too short to contain
            # the expected material information.
            if len(block.payload) < 14:
                continue

            texture = ""
            pos = 14

            # Check for an optional texture filename entry.
            # The value 3 identifies the expected string field.
            if (
                pos + 2 <= len(block.payload)
                and block.payload[pos] == 3
            ):
                n = block.payload[pos + 1]

                # Make sure the complete filename is present.
                if pos + 2 + n <= len(block.payload):
                    texture = block.payload[
                        pos + 2:pos + 2 + n
                    ].decode("ascii", "replace")

            # Store the material using its block name as the key.
            materials[block.name] = Material(
                block.name,
                texture
            )

        # ---------------------------------------------------------
        # Model block
        # ---------------------------------------------------------
        elif block.kind == 10:

            # Ignore the special body_sk block.
            # Other blocks update the currently active model name.
            if block.name != "body_sk":
                current_model = block.name

        # ---------------------------------------------------------
        # Shape / geometry block
        # ---------------------------------------------------------
        elif block.kind == 20:
            material = ""

            # If the following block is a material reference,
            # associate that material with the current mesh.
            if i + 1 < len(blocks) and blocks[i + 1].kind == 25:
                material = blocks[i + 1].name

            try:
                # Decode the shape and associate it with the
                # currently active model and selected material.
                mesh, nf, total = parse_shape(
                    block,
                    current_model,
                    material or block.name
                )

                # Add the successfully decoded mesh.
                meshes.append(mesh)

                # Compare the stored face count against the
                # total derived from the face descriptors.
                if nf != total:
                    warnings.append(
                        f"{block.name}: face count mismatch."
                    )

            # Record parsing errors without stopping the entire file.
            except (FormatError, struct.error) as exc:
                warnings.append(str(exc))

    # Warn if the file contained no usable mesh geometry.
    if not meshes:
        warnings.append("No usable geometry found.")

    # Return all extracted information in a document object.
    return MdlDocument(
        path,
        materials,
        meshes,
        warnings
    )


def safe_name(name):
    """
    Convert a name into a string suitable for OBJ/MTL identifiers.

    Characters other than letters, numbers, periods, underscores
    and hyphens are replaced with underscores.
    """
    return "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in name
    ) or "material"


def export_obj(doc, folder):
    """
    Export the parsed MDL document into Wavefront OBJ and MTL files.

    OBJ stores geometry, UV coordinates and face definitions.
    MTL stores material definitions.
    """

    # Create the destination folder if it does not already exist.
    folder.mkdir(parents=True, exist_ok=True)

    # Generate output filenames using the original MDL filename.
    obj = folder / (doc.path.stem + ".obj")
    mtl = folder / (doc.path.stem + ".mtl")

    # ---------------------------------------------------------
    # Write the OBJ geometry file.
    # ---------------------------------------------------------
    with obj.open("w", encoding="utf-8", newline="\n") as f:

        # Tell the OBJ file which material library to use.
        f.write(f"mtllib {mtl.name}\n")

        # OBJ indices start at 1 rather than 0.
        # These offsets keep indices correct across multiple meshes.
        vbase = 1
        uvbase = 1

        # Export each mesh separately.
        for mesh in doc.meshes:

            # Write an object declaration using the model and mesh names.
            f.write(
                f"\no {safe_name(mesh.model)}_"
                f"{safe_name(mesh.name)}\n"
            )

            # Assign the mesh's material.
            f.write(
                f"usemtl {safe_name(mesh.material)}\n"
            )

            # Write vertex positions.
            # Each vertex uses three coordinates: X, Y and Z.
            for x, y, z in mesh.vertices:
                f.write(f"v {x:.9g} {y:.9g} {z:.9g}\n")

            # Write UV texture coordinates.
            # The V coordinate is flipped using 1.0-v.
            for u, v in mesh.uvs:
                f.write(f"vt {u:.9g} {1.0-v:.9g}\n")

            # Write triangular faces.
            # OBJ face indices reference both vertex and UV indices.
            for a, b, c in mesh.faces:
                f.write(
                    f"f {vbase+a}/{uvbase+a} "
                    f"{vbase+b}/{uvbase+b} "
                    f"{vbase+c}/{uvbase+c}\n"
                )

            # Update offsets before exporting the next mesh.
            vbase += len(mesh.vertices)
            uvbase += len(mesh.uvs)

    # ---------------------------------------------------------
    # Write the MTL material library.
    # ---------------------------------------------------------
    with mtl.open("w", encoding="utf-8", newline="\n") as f:

        # Create a material definition for every extracted material.
        for name in doc.materials:
            f.write(
                f"\nnewmtl {safe_name(name)}\n"

                # Diffuse colour.
                "Kd 0.8 0.8 0.8\n"

                # Ambient colour.
                "Ka 0.2 0.2 0.2\n"

                # Specular colour.
                "Ks 0.1 0.1 0.1\n"
            )

            # Preserve the original texture filename as a comment.
            # This does not actively assign the texture in the MTL file.
            if doc.materials[name].texture:
                f.write(
                    f"# source texture: "
                    f"{doc.materials[name].texture}\n"
                )

    # Return the paths to both generated files.
    return obj, mtl


# =============================================================
# Graphical User Interface
# =============================================================

class Workbench(ctk.CTk):
    """
    Main application window for the MDL TO OBJ Converter.

    Allows the user to select an MDL file and export its
    geometry into OBJ and MTL formats.
    """

    def __init__(self):
        super().__init__()

        # Configure the main application window.
        self.title("MDL TO OBJ Converter")
        self.geometry("500x250")
        self.resizable(False, False)

        # Apply the dark appearance and blue colour theme.
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Store the selected source file and parsed document.
        self.source = None
        self.doc = None

        # Main instruction label.
        self.label = ctk.CTkLabel(
            self,
            text="Select a MDL file",
            font=ctk.CTkFont(size=16)
        )
        self.label.pack(pady=(35, 20))

        # Button used to browse for an MDL file.
        ctk.CTkButton(
            self,
            text="Browse MDL",
            command=self.choose
        ).pack(pady=8)

        # Button used to start the OBJ and MTL export process.
        ctk.CTkButton(
            self,
            text="Export OBJ + MTL",
            command=self.export
        ).pack(pady=8)

        # Status label used to display the current application state.
        self.status = ctk.CTkLabel(
            self,
            text="Ready"
        )
        self.status.pack(pady=15)

    def choose(self):
        """
        Open a file selection dialog and allow the user
        to choose a MDL file.
        """

        # Display a dialog filtered to MDL files.
        path = filedialog.askopenfilename(
            filetypes=[
                ("MDL files", "*.mdl"),
                ("All files", "*.*")
            ]
        )

        # Only update the application if a file was selected.
        if path:
            self.source = Path(path)

            # Clear any previously parsed document.
            self.doc = None

            # Display the selected filename in the interface.
            self.label.configure(text=self.source.name)

            # Update the status message.
            self.status.configure(text="File selected")

    def export(self):
        """
        Parse the selected MDL file and export its geometry.

        Displays warnings or error messages when appropriate.
        """

        # Make sure the user has selected a source file.
        if not self.source:
            messagebox.showwarning(
                "No file",
                "Select an MDL file first."
            )
            return

        try:
            # Parse the MDL file if it has not already been parsed.
            # Reuse the existing document if the user exports again.
            self.doc = self.doc or parse_mdl(self.source)

            # Prevent exporting a document without usable geometry.
            if not self.doc.meshes:
                raise FormatError("No usable geometry found.")

            # Ask the user where the exported files should be saved.
            folder = filedialog.askdirectory(
                title="Choose export folder",
                initialdir=str(self.source.parent)
            )

            # Stop if the user cancels the folder selection.
            if not folder:
                return

            # Generate the OBJ and MTL files in the chosen folder.
            obj, mtl = export_obj(
                self.doc,
                Path(folder)
            )

            # Update the interface to indicate successful completion.
            self.status.configure(text="Export complete")

            # Show the paths of the generated files.
            messagebox.showinfo(
                "Export complete",
                f"Created:\n{obj}\n{mtl}"
            )

        # Catch unexpected errors and display them to the user.
        except Exception as exc:
            self.status.configure(text="Export failed")

            messagebox.showerror(
                "Conversion failed",
                str(exc)
            )


# =============================================================
# Application Entry Point
# =============================================================

# Only launch the GUI when this script is executed directly.
# Importing this file as a module will not automatically open the window.
if __name__ == "__main__":
    Workbench().mainloop()
