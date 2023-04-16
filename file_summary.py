import os

def image_stats(f):
    # list all the files in the selected directory
    files=os.listdir(f)

    # make lists of all the files and locations of these files in a dictionary
    nef_details={}
    jpeg_details={}

    # make a location to store the jpegs if they aren't there
    if 'jpegs_temp' in files:
        temp_files=os.listdir(f+'/jpegs_temp')
        # list all files in the temp and their filepath if a temp already exists
        for i in temp_files:
            name=i.split('.')[0]
            if i.endswith('.NEF'):
                nef_details[name]=f+'/nef_temp'
            if i.endswith('.JPG'):
                jpeg_details[name]=f+'/jpegs_temp'
            if i.endswith('.nef'):
                nef_details[name]=f+'/nef_temp'
            if i.endswith('.jpg'):
                jpeg_details[name]=f+'/jpegs_temp'
        
    else:
        # make a temp if there isnt one
        os.mkdir(f+'/jpegs_temp')

    # list all the nefs and jpgs in the current directory and store the location of these
    for i in files:
        name=i.split('.')[0]
        if i.endswith('.NEF'):
            nef_details[name]=f
        if i.endswith('.JPG'):
            jpeg_details[name]=f
        if i.endswith('.nef'):
            nef_details[name]=f
        if i.endswith('.jpg'):
            jpeg_details[name]=f

    # find the list of combinations of files
    nef_not_jpeg=list(set(nef_details.keys())-set(jpeg_details.keys()))
    nef_and_jpeg=list(set(nef_details.keys()).intersection(set(jpeg_details.keys())))
    jpeg_not_nef=list(set(jpeg_details.keys())-set(nef_details.keys()))

    return(nef_not_jpeg,nef_and_jpeg,jpeg_not_nef,jpeg_details,nef_details)
