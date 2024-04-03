
import sys
import math
import re

def calc_execution_time(lines):
        try:              
            nMatchLines = {}
            currentFeedRate = 0
            currentX = 0
            currentY = 0
            
            for i, line in enumerate(lines):
              ##print(line)
              
              nLineInfo = {}
              deltaX = 0
              deltaY = 0
              newX = 0
              newY = 0
              
              match = re.search(r'^G1(\s(X)(\d*\.*\d*))*(\s(Y)(\d*\.*\d*))*(\s(E)((-)*\d*\.*\d*))*(\s(F)(\d+\.*\d*))*', line)
              if match:
                  nLineInfo["pos"] = i
                  for grp in match.groups():
                      if grp is not None:
                          if len(grp) > 1:
                              m = re.search(r'(X|Y|E|F)(-*\d*\.*\d*)',grp)
                              if m:
                                  nLineInfo[m.group(1)] = m.group(2)    
                                               
                  if "F" in nLineInfo:
                     currentFeedRate =  float(nLineInfo["F"])
                    ##print("Feedrate Found:",currentFeedRate)
                  
                  if "X" in nLineInfo:
                     deltaX = abs(float(nLineInfo["X"]) - currentX )
                     currentX = float(nLineInfo["X"])                 
                     ##print("DX:",deltaX)
        
                  if "Y" in nLineInfo:
                     deltaY = abs(float(nLineInfo["Y"]) - currentY)
                     currentY = float(nLineInfo["Y"])
                     ##print("DY:",deltaY)
                   
                  if deltaX > 0 or deltaY > 0:
                     mmPerSecond = currentFeedRate / 60 
                     segmentLength = math.sqrt(deltaX**2 + deltaY**2)
                     moveTime = float(segmentLength / mmPerSecond)
                     ##print("Time Calculation:",moveTime )  
                     nLineInfo["moveTime"] = moveTime
              nMatchLines[i] = nLineInfo     
                  
            return nMatchLines;
                
        except Exception as e:
            print(f"An error occurred: {str(e)}")
                  
    
def get_index_by_duration(nTimeCalculation,start_index, duration,direction):
    
    try:

        k=start_index

        totaltime = 0
        currentpos = k
        lineoffset = 0
        while totaltime < duration  and k > 0 and k < len(nTimeCalculation):
            if("moveTime" in nTimeCalculation[k]):
                totaltime= totaltime + nTimeCalculation[k]['moveTime']
                #currentpos = k
                lineoffset = lineoffset + 1
            if direction=="FORWARD":
                k = k + 1
            else:
                k = k - 1
            
        currentpos = k
        return currentpos
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")

def getNexToolUsageIndex(nTempMatches,tool):
    for key in nTempMatches:
        if int(nTempMatches[key][1]) == int(tool) :
            return nTempMatches[key][0]
    return -1

def process_toolchangerutils(file_path: str):
    try:
        predictive_offset_duration = 180  #seconds to calculate as offset to start heating toolhead       
        nextuse_duration = 180  #seconds to calculate as offset to start heating toolhead       


                    
                           
        with open(file_path, 'r') as file:
              lines = file.readlines()
    
        nTimeCalculation = calc_execution_time(lines)
         
        nMatches = {}
        nTempMatches={}
        k=0
        for i, line in enumerate(lines):
          match = re.search(r'^(T(\d))$', line)
          if match:
              nMatches[k] = [i,match.group(2)]
              nTempMatches[k] = [i,match.group(2)]
              k=k+1     
        
        k=0       
        currentTool = -1
        for key in nMatches:              
            compareIndex = 0
            if currentTool in nMatches:
                compareIndex = nMatches[key-1][0] + k;

            #print("Found T",nMatches[key][1],' at ',nMatches[key][0])
    
            insert_index = get_index_by_duration(nTimeCalculation,nMatches[key][0] ,predictive_offset_duration,"BACKWARD")
            #print("Insert Index ",insert_index)
            if insert_index > compareIndex :
               ##print("Insert Predictive heating for T",nMatches[key][1],' at ',insert_index)
               lines.insert(insert_index, "M568 P"+nMatches[key][1]+" A2 ;Predictive Heating\n")
               k=k+1
               
            if currentTool >= 0:
                nextToolUsageIndex = getNexToolUsageIndex(nTempMatches,currentTool)
                lineOffsetByDuration = get_index_by_duration(nTimeCalculation,nMatches[key][0]+k ,nextuse_duration,"FORWARD")
                #print("Next Tool Usage - T",currentTool," -- ",nextToolUsageIndex)
                #print("Next Tool Forward Time Index - T",currentTool," -- ",lineOffsetByDuration)
                
                if nextToolUsageIndex >=0 and lineOffsetByDuration > nextToolUsageIndex :
                    #print("Insert Continue heating for T",str(currentTool),' at ',lineOffsetByDuration)
                    insert_index_continue = nMatches[key][0] + 1 + k
                    lines.insert(insert_index_continue, "M568 P"+ str(currentTool) + " A2 ;Continue Heating\n")    
                    k=k+1
                #else:
                    #print("DONT INSERT Continue heating for T",nMatches[key][1],' - Next Tool Usage at ',nextToolUsageIndex, ' is before Time Index at', lineOffsetByDuration)                    
            
                
                
            currentTool = int(nMatches[key][1])         
            nTempMatches.pop(key)
               
   
        with open(file_path, 'w') as file:
           for i, line in enumerate(lines):
               file.write(line)
        
    except FileNotFoundError:
        print(f"File '{file_path}' not found.")
    except Exception as e:
        print(f"An error occurred: {str(e)}")

if __name__ == "__main__":
    process_toolchangerutils(sys.argv[1])