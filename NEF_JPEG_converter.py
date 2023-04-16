from PIL import Image
import rawpy
import imageio

def convert_nef_to_jpeg(f,nef_no_jpeg_list):
    # loop through the items in the list of nefs without jpegs
    for i in nef_no_jpeg_list:
        # open the raw image file
        with rawpy.imread(f+'/'+i+'.nef') as raw:
            # convert and save as jpg
            rgb = raw.postprocess(rawpy.Params(use_camera_wb=True))
            imageio.imsave(f+'/jpegs_temp/'+i+'.jpg',rgb)

    
 
    